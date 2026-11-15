"""Test doubles: plan builders, a stub schema provider and a scripted executor."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from nl2sql.models import ExecutionResult, PlanResult
from nl2sql.schema.provider import SchemaProvider

SetSettings = Callable[..., None]
Runner = Callable[..., Awaitable[dict[str, Any]]]


def sql_plan(sql: str, **kw: Any) -> PlanResult:
    return PlanResult(
        relevant_tables=kw.pop("relevant_tables", ["customers.customer_id"]),
        ambiguity_analysis=kw.pop("ambiguity_analysis", "None"),
        decision="sql",
        sql=sql,
        **kw,
    )


def clarify_plan(question: str) -> PlanResult:
    return PlanResult(ambiguity_analysis="ambiguous", decision="clarify", clarification=question)


def refuse_plan(reason: str) -> PlanResult:
    return PlanResult(ambiguity_analysis="n/a", decision="refuse", reason=reason)


class StubSchemaProvider(SchemaProvider):
    def __init__(
        self,
        context: str = "TABLE: customers (customer_id, name)",
        tables: frozenset[str] | None = None,
    ) -> None:
        self.context = context
        self.tables = tables
        self.calls = 0

    async def table_names(self) -> frozenset[str] | None:
        return self.tables

    async def get_context(self, question: str = "", evidence: str = "") -> str:
        self.calls += 1
        return self.context


def ok_result(rows: list[dict[str, Any]] | None = None) -> ExecutionResult:
    rows = rows if rows is not None else [{"total": 42}]
    cols = list(rows[0].keys()) if rows else []
    return ExecutionResult(success=True, columns=cols, rows=rows, row_count=len(rows))


def err_result(msg: str, *, safety: bool = False) -> ExecutionResult:
    return ExecutionResult(success=False, error=msg, is_safety_block=safety)


class FakeExecutor:
    """Drop-in for `execute_sql`: pops scripted ExecutionResults, repeats the last."""

    def __init__(self, *results: ExecutionResult) -> None:
        self.results = list(results)
        self.sqls: list[str] = []

    async def __call__(self, pool: Any, sql: str) -> ExecutionResult:
        self.sqls.append(sql)
        if len(self.results) > 1:
            return self.results.pop(0)
        return self.results[0] if self.results else ok_result()
