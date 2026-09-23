from __future__ import annotations

from pydantic import BaseModel, Field
from typing import Any, Literal


# ── Planner output ────────────────────────────────────────────────────────────

class PlanResult(BaseModel):
    """
    Structured output from the merged plan_sql node.

    Field ordering enforces DIN-SQL-style Chain-of-Thought:
    the LLM must populate relevant_tables and ambiguity_analysis
    BEFORE it is allowed to write sql, because tokens are generated
    left-to-right.  This eliminates silent semantic failures without
    any domain-specific hardcoding.

    Ref: Pourreza & Rafiei, "DIN-SQL: Decomposed In-Context Learning
    of Text-to-SQL with Self-Correction", NeurIPS 2023.
    """
    # ── Step 1: Schema Linking (DIN-SQL phase 1) ──────────────────────────────
    relevant_tables: list[str] = Field(
        default_factory=list,
        description=(
            "Flat list of 'table.column' strings for every table and column "
            "you will use in the query. Example: "
            "['orders.order_id', 'payments.amount', 'customers.name']. "
            "Do NOT nest objects here — only a plain list of strings. "
            "Always populate this field, even for refuse or clarify decisions "
            "(use an empty list [] only when truly no tables are relevant)."
        ),
    )

    # ── Step 2: Ambiguity Analysis (DIN-SQL phase 2, zero-shot) ──────────────
    ambiguity_analysis: str = Field(
        default="",
        description=(
            "Explicit reasoning about any schema or data ambiguities relevant to "
            "the question.  Consider: (a) multiple columns that could represent the "
            "same concept (e.g., two 'amount' columns), (b) nullable foreign keys "
            "that could silently drop rows in an INNER JOIN, (c) enum values that "
            "appear in the schema but are absent from the actual data profile, "
            "(d) duplicate natural keys (e.g., two customers with the same name). "
            "Write 'None' ONLY if the question is genuinely unambiguous after analysis. "
            "Always populate this field — never leave it empty."
        ),
    )

    # ── Step 3: Decision + SQL (only after CoT fields are populated) ──────────
    decision: Literal["sql", "clarify", "refuse"] = Field(
        description="What to do: generate SQL, ask for clarification, or refuse the request."
    )
    sql: str | None = Field(
        default=None,
        description="The generated SELECT SQL query. Required when decision='sql'."
    )
    assumptions: list[str] = Field(
        default_factory=list,
        description="List of assumptions made (e.g. 'Using payments.amount for revenue'). Always populate."
    )
    clarification: str | None = Field(
        default=None,
        description="The clarifying question to ask the user. Required when decision='clarify'."
    )
    reason: str | None = Field(
        default=None,
        description="Explanation for refusal. Required when decision='refuse'."
    )
    is_followup: bool = Field(
        default=False,
        description="True if this question references a previous turn's result."
    )


# ── SQL Validation ────────────────────────────────────────────────────────────

class SQLValidationResult(BaseModel):
    is_valid: bool
    is_blocked: bool = False   # True = terminal refusal, NOT retryable
    error: str | None = None

    @property
    def is_safety_block(self) -> bool:
        return self.is_blocked


# ── SQL Execution ─────────────────────────────────────────────────────────────

class ExecutionResult(BaseModel):
    success: bool
    columns: list[str] = Field(default_factory=list)
    rows: list[dict[str, Any]] = Field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    error: str | None = None
    is_safety_block: bool = False


# ── Final Agent Response (returned from every exit path) ─────────────────────

class AttemptRecord(BaseModel):
    attempt: int
    sql: str
    error: str
    classification: str


class AgentResponse(BaseModel):
    """Uniform response shape from every graph exit path."""
    status: Literal["answered", "needs_clarification", "refused", "failed"]
    answer: str | None = None
    sql: str | None = None
    columns: list[str] = Field(default_factory=list)
    rows: list[dict[str, Any]] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    attempts: list[AttemptRecord] = Field(default_factory=list)
    trace_id: str | None = None
    timings: dict[str, float] = Field(default_factory=dict)
