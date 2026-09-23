from __future__ import annotations

import json
import time

import structlog
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig

from nl2sql.agent.state import AgentState
from nl2sql.llm.providers import get_fast_llm

log = structlog.get_logger()

# Threshold for skipping LLM synthesis (template is used instead)
SMALL_TABLE_ROW_LIMIT = 10


async def synthesize_node(state: AgentState, config: RunnableConfig) -> dict:
    """
    Node 6: Hybrid answer synthesis.
    - Empty result     → template mentioning data date range
    - Single scalar    → template ("The answer is X")
    - ≤ 10 rows        → markdown table (no LLM call)
    - Larger result    → 8B LLM narration

    If status is already 'refused' or 'failed' (routed here for graceful failure),
    just format the failure message.
    """
    t0 = time.perf_counter()

    # Handle graceful failure path (max retries hit)
    if state.get("status") in ("refused", "failed"):
        return _format_failure(state, t0)

    rows = state.get("exec_rows", [])
    columns = state.get("exec_columns", [])
    question = state["question"]
    assumptions = state.get("assumptions", [])
    truncated = state.get("exec_truncated", False)

    # ── Empty result ──────────────────────────────────────────────────────────
    if not rows:
        answer = _empty_answer(question)
        return _build_output(state, answer, t0)

    # ── Single scalar ─────────────────────────────────────────────────────────
    if len(rows) == 1 and len(columns) == 1:
        val = rows[0][columns[0]]
        answer = f"**{val}**"
        if assumptions:
            answer += "\n\n> **Assumptions:** " + "; ".join(assumptions)
        return _build_output(state, answer, t0)

    # ── Small table — markdown template ───────────────────────────────────────
    if len(rows) <= SMALL_TABLE_ROW_LIMIT:
        answer = "### Results\n\n" + _markdown_table(columns, rows)
        if truncated:
            answer += f"\n\n*Showing first {len(rows)} rows — results were truncated.*"
        if assumptions:
            answer += "\n\n> **Assumptions:** " + "; ".join(assumptions)
        return _build_output(state, answer, t0)

    # ── Larger result — cheap LLM narration ──────────────────────────────────
    sample = rows[:20]
    prompt = (
        f"Question: {question}\n\n"
        f"SQL returned {state['exec_row_count']} rows. "
        f"Here are the first {len(sample)}:\n{json.dumps(sample, default=str)}\n\n"
        f"Assumptions made: {'; '.join(assumptions) if assumptions else 'None'}\n\n"
        "Summarise the key findings in 2-4 sentences using markdown formatting. "
        "Use **bold** for key numbers and findings. "
        "Do not invent numbers. Refer only to what is in the data above. "
        "Do NOT use emojis."
    )
    llm = get_fast_llm()
    try:
        response = await llm.ainvoke(
            [{"role": "user", "content": prompt}],
            config=config,
        )
        answer = response.content.strip()
        if truncated:
            answer += f"\n\n_(Showing first {state['exec_row_count']} of potentially more rows)_"
    except Exception as exc:
        log.error("synthesize_llm_error", error=str(exc))
        answer = _markdown_table(columns, rows[:10])

    return _build_output(state, answer, t0)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _empty_answer(question: str) -> str:
    base = "No rows matched your query."
    q_lower = question.lower()
    if any(kw in q_lower for kw in ["month", "week", "year", "date", "recent", "last", "today"]):
        base += (
            "\n\n**Note:** All order data in this database falls between "
            "**2026-01-01** and **2026-06-30**. Date filters outside this range will return no results."
        )
    return base


def _format_cell(val) -> str:
    """Format a cell value: round floats/Decimals to 2dp, leave strings/ints as-is."""
    import decimal
    if isinstance(val, float):
        return f"{val:,.2f}" if val != int(val) else str(int(val))
    if isinstance(val, decimal.Decimal):
        rounded = round(val, 2)
        # Add comma formatting for large numbers
        if abs(rounded) >= 1000:
            return f"{rounded:,.2f}"
        return str(rounded)
    return str(val) if val is not None else ""


def _markdown_table(columns: list[str], rows: list[dict]) -> str:
    header = "| " + " | ".join(columns) + " |"
    divider = "| " + " | ".join(["---"] * len(columns)) + " |"
    body_rows = []
    for row in rows:
        cells = [_format_cell(row.get(col)) for col in columns]
        body_rows.append("| " + " | ".join(cells) + " |")
    return "\n".join([header, divider] + body_rows)


def _build_output(state: AgentState, answer: str, t0: float) -> dict:
    elapsed_ms = round((time.perf_counter() - t0) * 1000)
    timings = dict(state.get("timings", {}))
    timings["synthesize_ms"] = elapsed_ms

    # Store turn context in last AIMessage for follow-up resolution
    turn_context = {
        "prev_question": state["question"],
        "prev_sql": state.get("generated_sql", ""),
        "prev_columns": state.get("exec_columns", []),
        "prev_row_count": state.get("exec_row_count", 0),
        "prev_sample_rows": json.dumps(state.get("exec_rows", [])[:5], default=str),
    }
    ai_msg = AIMessage(content=answer, additional_kwargs={"turn_context": turn_context})
    messages = list(state.get("messages", [])) + [ai_msg]

    return {
        "status": "answered",
        "final_answer": answer,
        "final_sql": state.get("generated_sql", ""),
        "messages": messages,
        "timings": timings,
    }


def _format_failure(state: AgentState, t0: float) -> dict:
    attempts = state.get("attempts", [])
    attempt_count = state.get("attempt_count", 0)
    answer = (
        f"**Query failed** after {attempt_count} attempt(s).\n\n"
        f"**Last error:** {attempts[-1]['error'] if attempts else 'Unknown'}\n\n"
        "Please try rephrasing your question or making it more specific."
    )
    elapsed_ms = round((time.perf_counter() - t0) * 1000)
    timings = dict(state.get("timings", {}))
    timings["synthesize_ms"] = elapsed_ms
    return {
        "status": "failed",
        "final_answer": answer,
        "final_sql": state.get("generated_sql", ""),
        "timings": timings,
    }
