from __future__ import annotations

import time
import uuid
from typing import Any, cast

import structlog
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig

from nl2sql.agent.state import AgentState
from nl2sql.agent.llm import planner_llm
from nl2sql.db.registry import DatabaseRegistry, UnknownDatabaseError
from nl2sql.llm.prompts.plan_sql import HINT_TEMPLATE, PLAN_SQL_SYSTEM
from nl2sql.models import PlanResult
from nl2sql.safety.validator import detect_prompt_injection

log = structlog.get_logger()


def refusal_text(db_display_name: str = "") -> str:
    """Generic refusal message that names the connected database when known."""
    if db_display_name:
        return f"I can only answer read-only data questions about the {db_display_name} database."
    return "I can only answer read-only data questions about the connected database."


async def load_schema_node(state: AgentState, registry: DatabaseRegistry) -> dict[str, Any]:
    """Node 1: resolve the request's database in the registry and load its schema context."""
    t0 = time.perf_counter()
    requested = state.get("db_id") or ""
    try:
        resources = await registry.get(requested or None)
    except UnknownDatabaseError as exc:
        msg = str(exc.args[0]) if exc.args else "Unknown database."
        return {
            "status": "failed",
            "final_answer": f"**Request failed:** {msg}",
            "final_sql": "",
            "refuse_reason": msg,
        }
    schema_context = await resources.schema_provider.get_context(state.get("question", ""))
    elapsed_ms = round((time.perf_counter() - t0) * 1000)
    timings = dict(state.get("timings", {}))
    timings["load_schema_ms"] = elapsed_ms
    log.info("load_schema_done", db_id=resources.db_id, duration_ms=elapsed_ms)
    profile = resources.profile
    return {
        "schema_context": schema_context,
        "db_id": resources.db_id,
        "db_display_name": profile.display_name if profile else "",
        "data_date_ranges": dict(profile.date_ranges) if profile else {},
        "trace_id": state.get("trace_id") or str(uuid.uuid4()),
        "timings": timings,
        "attempt_count": 0,
        "attempts": [],
        "assumptions": [],
    }


async def plan_sql_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """
    Node 2: Merged planner — one structured-output LLM call that:
    - Detects prompt injection → refuse
    - Detects write/off-topic intent → refuse
    - Detects genuine ambiguity → clarify
    - Detects follow-up → marks is_followup + generates SQL
    - Otherwise → generates SQL with stated assumptions
    """
    t0 = time.perf_counter()
    question = state["question"]

    # Fast-path: prompt injection detected before spending an LLM call
    if detect_prompt_injection(question):
        log.warning("prompt_injection_detected", question=question)
        return {
            "decision": "refuse",
            "refuse_reason": "Prompt injection attempt detected.",
            "status": "refused",
            "final_answer": refusal_text(state.get("db_display_name", "")),
        }

    # Build follow-up context from conversation history (app-level session store)
    followup_block = ""
    history = state.get("conversation_history", [])
    if history:
        followup_block = _build_history_block(history)

    evidence = (state.get("evidence") or "").strip()
    system_prompt = PLAN_SQL_SYSTEM.format(
        schema_context=state["schema_context"],
        hint_block=HINT_TEMPLATE.format(evidence=evidence) if evidence else "",
        followup_block=followup_block,
    )

    llm = planner_llm().with_structured_output(PlanResult, method="json_mode")

    try:
        result = cast(
            PlanResult,
            await llm.ainvoke(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": question},
                ],
                config=config,
            ),
        )
    except Exception as exc:
        elapsed_ms = round((time.perf_counter() - t0) * 1000)
        timings = dict(state.get("timings", {}))
        timings["plan_sql_ms"] = elapsed_ms
        log.error("plan_sql_llm_error", error=str(exc), duration_ms=elapsed_ms)
        return {
            "decision": "refuse",
            "refuse_reason": f"LLM error: {exc}",
            "status": "failed",
            "final_answer": f"The language model returned an error: {exc}",
            "llm_error": str(exc) or type(exc).__name__,
            "timings": timings,
        }

    elapsed_ms = round((time.perf_counter() - t0) * 1000)
    timings = dict(state.get("timings", {}))
    timings["plan_sql_ms"] = elapsed_ms

    # Update message history
    messages = state.get("messages", [])
    updated_messages = list(messages) + [HumanMessage(content=question)]

    log.info(
        "plan_sql_done",
        decision=result.decision,
        is_followup=result.is_followup,
        relevant_tables=result.relevant_tables,
        ambiguity_analysis=result.ambiguity_analysis,
        duration_ms=elapsed_ms,
    )

    return {
        "decision": result.decision,
        "generated_sql": result.sql or "",
        "assumptions": result.assumptions,
        "clarification_question": result.clarification or "",
        "refuse_reason": result.reason or "",
        "is_followup": result.is_followup,
        # DIN-SQL CoT fields — stored for telemetry / debugging
        "cot_relevant_tables": result.relevant_tables,
        "cot_ambiguity_analysis": result.ambiguity_analysis,
        "messages": updated_messages,
        "timings": timings,
    }


def _build_history_block(history: list[dict[str, Any]]) -> str:
    """Build a conversation history string from prior turns for prompt injection."""
    lines = ["━━━ CONVERSATION HISTORY (most recent last) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"]
    for i, turn in enumerate(history[-4:], 1):  # last 4 turns max
        lines.append(f"Turn {i}:")
        lines.append(f"  User: {turn.get('question', '')}")
        decision = turn.get("decision", "")
        if decision == "clarify":
            lines.append(f"  Agent (clarification asked): {turn.get('clarification', '')}")
        elif decision == "sql":
            lines.append(f"  Agent (SQL executed): {turn.get('sql', '')}")
            lines.append(f"  Agent (answer): {turn.get('answer', '')}")
        else:
            lines.append(f"  Agent: {turn.get('answer', '')}")
    lines.append(
        "Use this history to understand follow-up questions and references "
        "like 'their', 'those', 'the same', etc."
    )
    return "\n".join(lines) + "\n"
