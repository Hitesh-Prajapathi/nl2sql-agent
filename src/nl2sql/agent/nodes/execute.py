from __future__ import annotations

import time
from enum import StrEnum
from typing import Any

import structlog
from langchain_core.runnables import RunnableConfig

from nl2sql.agent.llm import planner_llm
from nl2sql.agent.state import AgentState, AttemptRecord
from nl2sql.config import get_settings
from nl2sql.db.executor import execute_sql
from nl2sql.db.registry import DatabaseRegistry
from nl2sql.llm.prompts.plan_sql import CORRECTION_SYSTEM
from nl2sql.models import ExecutionResult
from nl2sql.safety.validator import validate_sql

# Bounds on what the correction prompt repeats, so its size stays predictable (token-limited tiers).
MAX_CORRECTION_SQL_CHARS = 2000
MAX_CORRECTION_ERROR_CHARS = 600

log = structlog.get_logger()
settings = get_settings()


# ── Node 3: validate_sql ──────────────────────────────────────────────────────


def validate_sql_node(state: AgentState) -> dict[str, Any]:
    """Static AST validation — runs synchronously (no I/O)."""
    t0 = time.perf_counter()
    sql = state.get("generated_sql", "")
    result = validate_sql(sql)
    elapsed_ms = round((time.perf_counter() - t0) * 1000)
    timings = dict(state.get("timings", {}))
    timings["validate_sql_ms"] = elapsed_ms

    log.info(
        "validate_sql_done",
        is_valid=result.is_valid,
        is_blocked=result.is_blocked,
        error=result.error,
        duration_ms=elapsed_ms,
    )

    return {
        "is_valid_sql": result.is_valid,
        "is_blocked": result.is_blocked,
        "validation_error": result.error or "",
        "timings": timings,
    }


# ── Node 4: execute_sql ───────────────────────────────────────────────────────


async def execute_sql_node(state: AgentState, registry: DatabaseRegistry) -> dict[str, Any]:
    """Execute the SQL in a read-only transaction on the request's database."""
    t0 = time.perf_counter()
    result: ExecutionResult
    try:
        # `pool` may be None when tests stub `execute_sql` itself.
        pool: Any = (await registry.get(state.get("db_id") or None)).pool
    except Exception as exc:  # unknown database etc.: report as an execution error
        result = ExecutionResult(success=False, error=f"Database unavailable: {exc}")
    else:
        if state.get("eval_mode"):
            # Benchmark runs compare full result sets: lift the row cap.
            result = await execute_sql(pool, state["generated_sql"], row_cap=None)
        else:
            result = await execute_sql(pool, state["generated_sql"])
    elapsed_ms = round((time.perf_counter() - t0) * 1000)
    timings = dict(state.get("timings", {}))
    timings["execute_sql_ms"] = elapsed_ms

    log.info(
        "execute_sql_done",
        success=result.success,
        row_count=result.row_count,
        truncated=result.truncated,
        duration_ms=elapsed_ms,
    )

    return {
        "exec_columns": result.columns,
        "exec_rows": result.rows,
        "exec_row_count": result.row_count,
        "exec_truncated": result.truncated,
        "exec_error": result.error or "",
        "is_safety_exec_block": result.is_safety_block,
        "timings": timings,
    }


# ── Node 5: correct_sql ───────────────────────────────────────────────────────


class ErrorClass(StrEnum):
    SYNTAX = "syntax"  # Retryable: column not found, type mismatch, parse error
    TIMEOUT = "timeout"  # Retryable: instruct "simplify or narrow query"
    SAFETY = "safety"  # TERMINAL: write blocked by DB role or transaction
    UNKNOWN = "unknown"  # Retryable: catch-all


def _classify_error(error: str, is_safety_block: bool) -> ErrorClass:
    if is_safety_block:
        return ErrorClass.SAFETY
    el = error.lower()
    if "statement_timeout" in el or "query canceled" in el or "query cancelled" in el:
        return ErrorClass.TIMEOUT
    if "permission denied" in el or "read-only" in el:
        return ErrorClass.SAFETY
    return ErrorClass.SYNTAX


CORRECTION_INSTRUCTIONS = {
    ErrorClass.SYNTAX: (
        "Fix the specific SQL error shown above. "
        "Pay attention to column names, table names, and join conditions."
    ),
    ErrorClass.TIMEOUT: (
        "The query timed out. Simplify it: reduce the number of JOINs, "
        "remove expensive subqueries, add a LIMIT clause, or narrow the date range."
    ),
    ErrorClass.UNKNOWN: "Try a different approach to answer the same question.",
}


async def correct_sql_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """
    Error-classified self-correction:
    - SAFETY errors are TERMINAL (no retry, no LLM call)
    - TIMEOUT errors get a 'simplify' instruction
    - All others get a 'fix the specific error' instruction
    """
    attempt = state.get("attempt_count", 0) + 1
    error = state.get("exec_error") or state.get("validation_error") or "Unknown error"
    is_safety = state.get("is_safety_exec_block", False) or state.get("is_blocked", False)

    error_class = _classify_error(error, is_safety)

    # Record this attempt
    attempt_record: AttemptRecord = {
        "attempt": attempt,
        "sql": state.get("generated_sql", ""),
        "error": error,
        "classification": error_class.value,
    }
    attempts = list(state.get("attempts", [])) + [attempt_record]

    log.warning(
        "sql_error_classified",
        attempt=attempt,
        classification=error_class.value,
        error=error[:200],
    )

    if error_class == ErrorClass.SAFETY:
        return {
            "attempt_count": attempt,
            "attempts": attempts,
            "status": "refused",
            "final_answer": (
                "This operation was blocked for safety reasons. "
                "Only read-only SELECT queries are permitted."
            ),
            "final_sql": state.get("generated_sql", ""),
        }

    # Retry budget: max_retry_attempts = number of correction LLM calls allowed.
    # Check BEFORE spending an LLM call; 0 disables correction entirely.
    if state.get("attempt_count", 0) >= settings.max_retry_attempts:
        log.warning("correction_budget_exhausted", budget=settings.max_retry_attempts)
        return {
            "status": "failed",
            "final_answer": f"**Query failed.** Last error: {error}",
            "final_sql": state.get("generated_sql", ""),
        }

    # Generate corrected SQL via LLM
    correction_instruction = CORRECTION_INSTRUCTIONS[error_class]
    system_prompt = CORRECTION_SYSTEM.format(
        question=state["question"],
        failed_sql=state.get("generated_sql", "")[:MAX_CORRECTION_SQL_CHARS],
        error=error[:MAX_CORRECTION_ERROR_CHARS],
        correction_instruction=correction_instruction,
        schema_context=state["schema_context"],
    )

    llm = planner_llm()
    llm_error = ""
    try:
        response = await llm.ainvoke(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": "Return ONLY the corrected SQL query."},
            ],
            config=config,
        )
        corrected_sql = str(response.content).strip()
        # Strip markdown fences if the model added them
        if corrected_sql.startswith("```"):
            corrected_sql = corrected_sql.split("```")[1]
            if corrected_sql.lower().startswith("sql"):
                corrected_sql = corrected_sql[3:].strip()
    except Exception as exc:
        log.error("correct_sql_llm_error", error=str(exc))
        corrected_sql = state.get("generated_sql", "")
        llm_error = str(exc) or type(exc).__name__

    return {
        "llm_error": llm_error,
        "attempt_count": attempt,
        "attempts": attempts,
        "generated_sql": corrected_sql,
        "correction_context": correction_instruction,
        # Reset exec state
        "exec_error": "",
        "is_safety_exec_block": False,
        "is_blocked": False,
    }
