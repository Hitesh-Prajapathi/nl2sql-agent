from __future__ import annotations

import functools
from typing import Any

import asyncpg
import structlog
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import BaseMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph

from nl2sql.agent.nodes.execute import (
    correct_sql_node,
    execute_sql_node,
    validate_sql_node,
)
from nl2sql.agent.nodes.plan import load_schema_node, plan_sql_node
from nl2sql.agent.nodes.synthesize import synthesize_node
from nl2sql.agent.state import AgentState
from nl2sql.config import get_settings
from nl2sql.db.registry import DatabaseRegistry
from nl2sql.schema.provider import SchemaProvider

log = structlog.get_logger()
settings = get_settings()


# ── Routing functions ─────────────────────────────────────────────────────────


def route_after_load(state: AgentState) -> str:
    """An unknown database terminates the run (status=failed) before any LLM call."""
    return "end" if state.get("status") == "failed" else "plan"


def route_after_plan(state: AgentState) -> str:
    decision = state.get("decision")
    if decision == "clarify":
        return "clarify"
    if decision == "refuse":
        return "refuse"
    return "validate"


def _retry_budget_exhausted(state: AgentState) -> bool:
    """
    Retry-budget rule (single source of truth):
    ``max_retry_attempts`` = number of correction LLM calls allowed.
    ``attempt_count`` = correction LLM calls made so far. A correction is
    allowed iff attempt_count < max_retry_attempts, so MAX_RETRY_ATTEMPTS=0
    disables correction entirely.
    """
    return state.get("attempt_count", 0) >= settings.max_retry_attempts


def route_after_validation(state: AgentState) -> str:
    if state.get("is_blocked"):
        return "blocked"  # Terminal refusal (-> refuse_node), no retry
    if not state.get("is_valid_sql"):
        # Retryable syntax error — correct if budget remains, else fail
        if _retry_budget_exhausted(state):
            return "max_retries"
        return "correct"  # Feed validation error into correction node
    return "execute"


def route_after_execution(state: AgentState) -> str:
    # Budget is enforced by correct_sql_node (before its LLM call); it also
    # needs to see safety blocks even when the budget is 0.
    if state.get("exec_error"):
        return "error"
    return "success"


def route_after_correction(state: AgentState) -> str:
    if state.get("status") == "refused":
        return "end"  # Safety block — terminal
    if (
        state.get("status") == "failed"
        or state.get("attempt_count", 0) > settings.max_retry_attempts
    ):
        return "fail"  # Budget exhausted before the LLM call -> max_retries_node
    return "validate"  # A correction was made: validate (and run) its SQL


# ── Clarification / refuse terminal nodes ────────────────────────────────────


def clarify_node(state: AgentState) -> dict[str, Any]:
    question = state.get("clarification_question", "Could you please clarify your question?")
    return {
        "status": "needs_clarification",
        "final_answer": f"**Clarification needed:** {question}",
        "final_sql": None,
    }


def refuse_node(state: AgentState) -> dict[str, Any]:
    if state.get("is_blocked"):
        # AST validator block (B1): carry the validation error into the answer
        reason = (
            f"Query blocked by safety validation: {state.get('validation_error') or 'unsafe SQL'}"
        )
    else:
        reason = state.get("refuse_reason") or "This request cannot be processed."
    # Preserve 'failed' status if already set by LLM error path
    existing_status = state.get("status")
    status = existing_status if existing_status == "failed" else "refused"
    answer = state.get("final_answer") or f"**Request refused:** {reason}"
    return {
        "status": status,
        "final_answer": answer,
        "final_sql": None,
    }


def max_retries_node(state: AgentState) -> dict[str, Any]:
    """Terminal failure after the correction budget is spent (or disabled)."""
    attempts = state.get("attempts", [])
    last_error = (
        state.get("exec_error")
        or state.get("validation_error")
        or (attempts[-1]["error"] if attempts else "")
        or "Unknown error"
    )
    n = state.get("attempt_count", 0)
    return {
        "status": "failed",
        "final_answer": (
            f"**Query failed** after {n} correction attempt(s).\n\n"
            f"**Last error:** {last_error}\n\n"
            "Please try rephrasing your question or making it more specific."
        ),
        "final_sql": state.get("generated_sql", ""),
    }


# ── Graph builder ─────────────────────────────────────────────────────────────


def build_graph(
    pool: asyncpg.Pool | None = None,
    schema_provider: SchemaProvider | None = None,
    *,
    registry: DatabaseRegistry | None = None,
) -> CompiledStateGraph[AgentState, Any, AgentState, AgentState]:
    """
    Build and compile the NL-to-SQL LangGraph.

    Graph topology:
      load_schema → plan_sql → {clarify|refuse → END, validate}
                            validate → {blocked → refuse, max_retries, execute|correct}
                            execute → {success → synthesize, error → correct}
                            correct → {end (safety), fail → max_retries, validate (retry)}
                            max_retries → END (status=failed); synthesize → END

    synthesize is reachable ONLY after a successful execute.

    Resources are looked up per request: the state's ``db_id`` selects a (pool, schema provider,
    profile) from the ``DatabaseRegistry``. Without a registry, the legacy single-database call
    ``build_graph(pool[, schema_provider])`` is wrapped into a one-entry registry, so the
    default database keeps working unchanged.
    """
    if registry is None:
        registry = DatabaseRegistry.from_single(pool, schema_provider)

    builder = StateGraph(AgentState)

    # ── Register nodes ────────────────────────────────────────────────────────
    builder.add_node(
        "load_schema",
        functools.partial(load_schema_node, registry=registry),
    )
    builder.add_node("plan_sql", plan_sql_node)
    builder.add_node("clarify", clarify_node)
    builder.add_node("refuse", refuse_node)
    builder.add_node("validate_sql", validate_sql_node)
    builder.add_node("max_retries", max_retries_node)
    builder.add_node(
        "execute_sql",
        functools.partial(execute_sql_node, registry=registry),
    )
    builder.add_node("correct_sql", correct_sql_node)
    builder.add_node("synthesize", synthesize_node)

    # ── Entry point ───────────────────────────────────────────────────────────
    builder.set_entry_point("load_schema")
    builder.add_conditional_edges("load_schema", route_after_load, {"plan": "plan_sql", "end": END})

    # plan_sql → clarify | refuse | validate_sql
    builder.add_conditional_edges(
        "plan_sql",
        route_after_plan,
        {"clarify": "clarify", "refuse": "refuse", "validate": "validate_sql"},
    )
    builder.add_edge("clarify", END)
    builder.add_edge("refuse", END)

    # validate_sql → blocked (refuse) | max_retries | correct | execute_sql
    builder.add_conditional_edges(
        "validate_sql",
        route_after_validation,
        {
            "blocked": "refuse",
            "max_retries": "max_retries",
            "correct": "correct_sql",
            "execute": "execute_sql",
        },
    )
    builder.add_edge("max_retries", END)

    # execute_sql → synthesize | correct_sql
    builder.add_conditional_edges(
        "execute_sql",
        route_after_execution,
        {"success": "synthesize", "error": "correct_sql"},
    )

    # correct_sql → end (safety) | fail (max_retries) | validate_sql (retry)
    builder.add_conditional_edges(
        "correct_sql",
        route_after_correction,
        {"end": END, "fail": "max_retries", "validate": "validate_sql"},
    )

    builder.add_edge("synthesize", END)

    return builder.compile()


# ── Convenience run function ──────────────────────────────────────────────────


async def run_query(
    graph: CompiledStateGraph[AgentState, Any, AgentState, AgentState],
    question: str,
    thread_id: str = "default",
    messages: list[BaseMessage] | None = None,
    conversation_history: list[dict[str, Any]] | None = None,
    callbacks: list[BaseCallbackHandler] | None = None,
    *,
    db_id: str = "",
    eval_mode: bool = False,
    evidence: str = "",
) -> dict[str, Any]:
    """Invoke the graph and return the final state."""
    initial_state: AgentState = {
        "question": question,
        "thread_id": thread_id,
        "messages": messages or [],
        "conversation_history": conversation_history or [],
        "schema_context": "",
        "decision": None,
        "assumptions": [],
        "clarification_question": "",
        "refuse_reason": "",
        "is_followup": False,
        "generated_sql": "",
        "cot_relevant_tables": [],
        "cot_ambiguity_analysis": "",
        "is_valid_sql": False,
        "is_blocked": False,
        "validation_error": "",
        "exec_columns": [],
        "exec_rows": [],
        "exec_row_count": 0,
        "exec_truncated": False,
        "exec_error": "",
        "is_safety_exec_block": False,
        "attempt_count": 0,
        "correction_context": "",
        "attempts": [],
        "status": "answered",
        "final_answer": "",
        "final_sql": "",
        "trace_id": "",
        "timings": {},
        # Populated by load_schema_node from the schema provider's DatabaseProfile
        "db_id": db_id,
        "eval_mode": eval_mode,
        "evidence": evidence,
        "llm_error": "",
        "db_display_name": "",
        "data_date_ranges": {},
    }

    clean_name = question.strip().replace("\n", " ")
    trace_name = clean_name if len(clean_name) <= 60 else f"{clean_name[:57]}..."

    config: RunnableConfig = {
        "configurable": {"thread_id": thread_id},
        "metadata": {
            "langfuse_session_id": thread_id,
            "langfuse_trace_name": trace_name,
            "agent_version": settings.agent_version,
        },
    }
    if callbacks:
        config["callbacks"] = callbacks
    return await graph.ainvoke(initial_state, config=config)
