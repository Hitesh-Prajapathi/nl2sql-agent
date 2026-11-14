"""Evaluation variants.

``full``  the agent graph as shipped (profiling, schema-linking CoT planner, validation,
          self-correction) in eval mode (no synthesis, uncapped rows).
``naive`` B0 baseline: ONE prompt containing the raw DDL (+ optional evidence) and the question,
          same planner model, no profiling, no CoT, no validation, no correction.
"""

from __future__ import annotations

import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from langgraph.graph.state import CompiledStateGraph

from nl2sql.agent.graph import run_query
from nl2sql.agent.llm import llm_hook, planner_llm
from nl2sql.agent.state import AgentState
from nl2sql.eval.dataset import Question
from nl2sql.eval.llm_hook import EvalLLMHook, UsageCollector, current_usage

Graph = CompiledStateGraph[AgentState, Any, AgentState, AgentState]

NAIVE_SYSTEM = (
    "You are a PostgreSQL expert. Given the database schema below, write one SQL query that "
    "answers the user's question. Return ONLY the SQL query, with no explanation and no "
    "markdown fences.\n\nSCHEMA (PostgreSQL DDL):\n{ddl}\n"
)
NAIVE_HINT = "\nHint (external knowledge about the question): {evidence}\n"

INFRA_PATTERN = re.compile(r"429|rate.?limit|timed? ?out|unavailable|connection|5\d\d", re.I)


@dataclass
class Prediction:
    status: str  # answered | needs_clarification | refused | failed | infra_error
    sql: str = ""
    decision: str | None = None
    failed_attempts: int = 0  # correction attempts recorded after a failed execution/validation
    relevant_tables: list[str] = field(default_factory=list)
    usage: dict[str, dict[str, dict[str, int]]] = field(default_factory=dict)
    latency_s: float = 0.0
    llm_error: str = ""
    error: str = ""  # last agent-side error text (execution / validation / refusal reason)
    exec_row_count: int = 0
    exec_truncated: bool = False
    node_timings_ms: dict[str, float] = field(default_factory=dict)


class Variant(Protocol):
    name: str

    async def predict(self, q: Question, use_evidence: bool) -> Prediction: ...


def is_rate_limit(text: str) -> bool:
    return "429" in text or "rate" in text.lower()


class FullVariant:
    name = "full"

    def __init__(self, graph: Graph, hook: EvalLLMHook) -> None:
        self._graph = graph
        self._hook = hook

    async def predict(self, q: Question, use_evidence: bool) -> Prediction:
        collector = UsageCollector()
        tok_u = current_usage.set(collector)
        tok_h = llm_hook.set(self._hook)
        t0 = time.perf_counter()
        try:
            state = await run_query(
                self._graph,
                q.question,
                thread_id=f"eval-{q.question_id}",
                db_id=q.db_id,
                eval_mode=True,
                evidence=q.evidence if use_evidence else "",
            )
        except Exception as exc:
            return Prediction(
                status="infra_error",
                llm_error=f"{type(exc).__name__}: {exc}",
                latency_s=time.perf_counter() - t0,
                usage=collector.to_dict(),
            )
        finally:
            current_usage.reset(tok_u)
            llm_hook.reset(tok_h)
        latency = time.perf_counter() - t0
        attempts = state.get("attempts") or []
        last_err = (attempts[-1]["error"] if attempts else "") or state.get("validation_error", "")
        llm_error = str(state.get("llm_error") or "")
        status = "infra_error" if llm_error else str(state.get("status", "failed"))
        return Prediction(
            status=status,
            sql=str(state.get("final_sql") or state.get("generated_sql") or ""),
            decision=state.get("decision"),
            failed_attempts=len(attempts),
            relevant_tables=list(state.get("cot_relevant_tables") or []),
            usage=collector.to_dict(),
            latency_s=latency,
            llm_error=llm_error,
            error=str(state.get("exec_error") or last_err or state.get("refuse_reason") or ""),
            exec_row_count=int(state.get("exec_row_count", 0)),
            exec_truncated=bool(state.get("exec_truncated", False)),
            node_timings_ms=dict(state.get("timings") or {}),
        )


_FENCE = re.compile(r"^```(?:sql)?\s*|\s*```$", re.I)


def clean_sql(text: str) -> str:
    return _FENCE.sub("", text.strip()).strip().rstrip(";").strip()


class NaiveVariant:
    name = "naive"

    def __init__(self, ddl_by_db: dict[str, str], hook: EvalLLMHook) -> None:
        self._ddl = ddl_by_db
        self._hook = hook

    async def predict(self, q: Question, use_evidence: bool) -> Prediction:
        ddl = self._ddl.get(q.db_id)
        if ddl is None:
            return Prediction(status="failed", error=f"no DDL for database {q.db_id!r}")
        system = NAIVE_SYSTEM.format(ddl=ddl)
        if use_evidence and q.evidence:
            system += NAIVE_HINT.format(evidence=q.evidence)
        collector = UsageCollector()
        tok_u = current_usage.set(collector)
        tok_h = llm_hook.set(self._hook)
        t0 = time.perf_counter()
        try:
            llm = planner_llm()
            resp = await llm.ainvoke(
                [{"role": "system", "content": system}, {"role": "user", "content": q.question}],
                {"metadata": {"langgraph_node": "naive_sql"}},
            )
            sql = clean_sql(str(resp.content))
        except Exception as exc:
            return Prediction(
                status="infra_error",
                llm_error=f"{type(exc).__name__}: {exc}",
                latency_s=time.perf_counter() - t0,
                usage=collector.to_dict(),
            )
        finally:
            current_usage.reset(tok_u)
            llm_hook.reset(tok_h)
        return Prediction(
            status="answered" if sql else "failed",
            sql=sql,
            decision="sql" if sql else None,
            usage=collector.to_dict(),
            latency_s=time.perf_counter() - t0,
            error="" if sql else "empty model output",
        )


def build_ddl(profile: dict[str, Any]) -> str:
    """Raw CREATE TABLE statements (columns, types, PK, FK) from a stored catalog profile.

    Deliberately no sample rows, value lists or ranges: that is what the naive baseline lacks.
    """
    parts: list[str] = []
    for t in profile["tables"]:
        lines = [f'  "{c["name"]}" {c["type"]}{"" if c["nullable"] else " NOT NULL"}' for c in t["columns"]]
        if t.get("primary_key"):
            lines.append("  PRIMARY KEY (" + ", ".join(f'"{c}"' for c in t["primary_key"]) + ")")
        for fk in t.get("foreign_keys", []):
            lines.append(
                f'  FOREIGN KEY ("{fk["column"]}") REFERENCES "{fk["foreign_table"]}"("{fk["foreign_column"]}")'
            )
        parts.append(f'CREATE TABLE "{t["name"]}" (\n' + ",\n".join(lines) + "\n);")
    return "\n\n".join(parts)


VariantFactory = Callable[[], Awaitable[Variant]]
