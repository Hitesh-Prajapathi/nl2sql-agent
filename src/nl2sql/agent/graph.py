from __future__ import annotations

import functools

import asyncpg
import structlog
from langchain_core.messages import BaseMessage
from langgraph.graph import END, StateGraph

from nl2sql.agent.nodes.execute import (
    correct_sql_node,
    execute_sql_node,
    validate_sql_node,
)
from nl2sql.agent.nodes.plan import load_schema_node, plan_sql_node
from nl2sql.agent.nodes.synthesize import synthesize_node
from nl2sql.agent.state import AgentState
from nl2sql.config import get_settings
from nl2sql.schema.provider import FullSchemaProvider, SchemaProvider

log = structlog.get_logger()
settings = get_settings()


# ── Routing functions ─────────────────────────────────────────────────────────

def route_after_plan(state: AgentState) -> str:
    decision = state.get("decision")
    if decision == "clarify":
        return "clarify"
    if decision == "refuse":
        return "refuse"
    return "validate"


def route_after_validation(state: AgentState) -> str:
    if state.get("is_blocked"):
        return "blocked"   # Terminal refusal, no retry
    if not state.get("is_valid_sql"):
        # Retryable syntax error — increment attempt and route back
        attempt = state.get("attempt_count", 0)
        if attempt >= settings.max_retry_attempts:
            return "max_retries"
        return "correct"   # Feed validation error into correction node
    return "execute"


def route_after_execution(state: AgentState) -> str:
    if state.get("exec_error"):
        return "error"
    return "success"


def route_after_correction(state: AgentState) -> str:
    if state.get("status") == "refused":
        return "end"   # Safety block — terminal
    if state.get("attempt_count", 0) >= settings.max_retry_attempts:
        return "fail"
    return "validate"  # Route back through validation


# ── Clarification / refuse terminal nodes ────────────────────────────────────

def clarify_node(state: AgentState) -> dict:
    question = state.get("clarification_question", "Could you please clarify your question?")
    return {
        "status": "needs_clarification",
        "final_answer": f"**Clarification needed:** {question}",
        "final_sql": None,
    }


def refuse_node(state: AgentState) -> dict:
    reason = state.get("refuse_reason", "This request cannot be processed.")
    # Preserve 'failed' status if already set by LLM error path
    existing_status = state.get("status")
    status = existing_status if existing_status == "failed" else "refused"
    answer = state.get("final_answer") or f"**Request refused:** {reason}"
    return {
        "status": status,
        "final_answer": answer,
        "final_sql": None,
    }


def max_retries_node(state: AgentState) -> dict:
    return {
        "status": "failed",
        "final_answer": (
            f"**Query failed** after {settings.max_retry_attempts} attempt(s). "
            "Please try rephrasing your question."
        ),
    }


# ── Graph builder ─────────────────────────────────────────────────────────────

def build_graph(pool: asyncpg.Pool) -> StateGraph:
    """
    Build and compile the NL-to-SQL LangGraph.

    Graph topology:
      load_schema → plan_sql → {clarify|refuse → END, validate}
                            validate → {blocked|max_retries → END, execute|correct}
                            execute → {success → synthesize, error → correct}
                            correct → {end (safety), fail → synthesize, validate (retry)}
                            synthesize → END
    """
    schema_provider: SchemaProvider = FullSchemaProvider(pool)

    builder = StateGraph(AgentState)

    # ── Register nodes ────────────────────────────────────────────────────────
    builder.add_node(
        "load_schema",
        functools.partial(load_schema_node, schema_provider=schema_provider),
    )
    builder.add_node("plan_sql", plan_sql_node)
    builder.add_node("clarify", clarify_node)
    builder.add_node("refuse", refuse_node)
    builder.add_node("validate_sql", validate_sql_node)
    builder.add_node("max_retries", max_retries_node)
    builder.add_node(
        "execute_sql",
        functools.partial(execute_sql_node, pool=pool),
    )
    builder.add_node("correct_sql", correct_sql_node)
    builder.add_node("synthesize", synthesize_node)

    # ── Entry point ───────────────────────────────────────────────────────────
    builder.set_entry_point("load_schema")
    builder.add_edge("load_schema", "plan_sql")

    # plan_sql → clarify | refuse | validate_sql
    builder.add_conditional_edges(
        "plan_sql",
        route_after_plan,
        {"clarify": "clarify", "refuse": "refuse", "validate": "validate_sql"},
    )
    builder.add_edge("clarify", END)
    builder.add_edge("refuse", END)

    # validate_sql → blocked (END) | max_retries (END) | correct | execute_sql
    builder.add_conditional_edges(
        "validate_sql",
        route_after_validation,
        {
            "blocked": END,
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

    # correct_sql → end (safety) | fail (synthesize) | validate_sql (retry)
    builder.add_conditional_edges(
        "correct_sql",
        route_after_correction,
        {"end": END, "fail": "synthesize", "validate": "validate_sql"},
    )

    builder.add_edge("synthesize", END)

    return builder.compile()


# ── Convenience run function ──────────────────────────────────────────────────

async def run_query(
    graph,
    question: str,
    thread_id: str = "default",
    messages: list[BaseMessage] | None = None,
    conversation_history: list[dict] | None = None,
    callbacks: list | None = None,
) -> dict:
    """Invoke the graph and return the final state."""
    initial_state: AgentState = {  # type: ignore[assignment]
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
    }

    config: dict = {"configurable": {"thread_id": thread_id}}
    if callbacks:
        config["callbacks"] = callbacks
    return await graph.ainvoke(initial_state, config=config)
