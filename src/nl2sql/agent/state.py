from __future__ import annotations

from typing import Literal

from langchain_core.messages import BaseMessage
from typing_extensions import TypedDict


class AttemptRecord(TypedDict):
    attempt: int
    sql: str
    error: str
    classification: str


class AgentState(TypedDict):
    # ── Input ─────────────────────────────────────────────────────────────────
    messages: list[BaseMessage]          # Full conversation history
    question: str                        # Current user question
    thread_id: str                       # Session ID for multi-turn memory
    conversation_history: list[dict]     # Serialisable prior turns for prompt injection

    # ── Schema ────────────────────────────────────────────────────────────────
    schema_context: str                  # Dynamically introspected DDL + profiling

    # ── Planning ──────────────────────────────────────────────────────────────
    decision: Literal["sql", "clarify", "refuse"] | None
    assumptions: list[str]               # Stated assumptions ("Using payments.amount for revenue")
    clarification_question: str          # Question to ask user if decision = "clarify"
    refuse_reason: str                   # Reason if decision = "refuse"
    is_followup: bool                    # Whether this references a previous turn

    # ── SQL Generation & Validation ────────────────────────────────────────
    generated_sql: str                   # Current SQL attempt
    # DIN-SQL CoT fields (logged for observability; not sent to user)
    cot_relevant_tables: list[str]       # Step 1: schema-linked tables/columns
    cot_ambiguity_analysis: str          # Step 2: ambiguity reasoning prose
    is_valid_sql: bool                   # sqlglot validation result
    is_blocked: bool                     # True = terminal (write/dangerous), no retry
    validation_error: str                # Static validation error message

    # ── Execution ─────────────────────────────────────────────────────────────
    exec_columns: list[str]              # Result column names
    exec_rows: list[dict]                # Result rows (up to ROW_CAP)
    exec_row_count: int                  # Actual row count returned
    exec_truncated: bool                 # True if results were capped
    exec_error: str                      # PostgreSQL error message
    is_safety_exec_block: bool           # True if PG itself rejected the query

    # ── Self-Correction ───────────────────────────────────────────────────────
    attempt_count: int                   # Current retry attempt (0-indexed)
    correction_context: str             # Error-specific instruction for re-generation
    attempts: list[AttemptRecord]        # Full retry history

    # ── Output ────────────────────────────────────────────────────────────────
    status: Literal["answered", "needs_clarification", "refused", "failed"]
    final_answer: str
    final_sql: str

    # ── Telemetry ─────────────────────────────────────────────────────────────
    trace_id: str
    timings: dict[str, float]            # Per-node latency breakdown (ms)
