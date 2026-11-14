"""Evaluation variants.

``full``  the agent graph as shipped (profiling, schema-linking CoT planner, validation,
          self-correction) in eval mode (no synthesis, uncapped rows).
``naive`` B0 baseline: ONE prompt containing the raw DDL (+ optional evidence) and the question,
          same planner model, no profiling, no CoT, no validation, no correction.
``full_small`` / ``naive_small``  the same two, but the schema text is chosen per question
          (lexical relevance + FK neighbours) so that prompt + output tokens stay under a hard
          cap, for providers with a small per-minute token limit (Groq free tier: 8,000/min).
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
from nl2sql.llm.tokens import estimate_messages_tokens
from nl2sql.schema.ddl import build_ddl_capped

Graph = CompiledStateGraph[AgentState, Any, AgentState, AgentState]

NAIVE_SYSTEM = (
    "You are a PostgreSQL expert. Given the database schema below, write one SQL query that "
    "answers the user's question. Return ONLY the SQL query, with no explanation and no "
    "markdown fences.\n\nSCHEMA (PostgreSQL DDL):\n{ddl}\n"
)
NAIVE_HINT = "\nHint (external knowledge about the question): {evidence}\n"

VARIANTS = ("full", "naive", "full_small", "naive_small")
SMALL_VARIANTS = ("full_small", "naive_small")

INFRA_PATTERN = re.compile(r"429|rate.?limit|timed? ?out|unavailable|connection|5\d\d", re.I)


@dataclass
class Prediction:
    status: str  # answered | needs_clarification | refused | failed | infra_error
    sql: str = ""
    decision: str | None = None
    failed_attempts: int = 0  # correction attempts recorded after a failed execution/validation
    relevant_tables: list[str] = field(default_factory=list)
    usage: dict[str, dict[str, dict[str, int]]] = field(default_factory=dict)
    llm_calls: list[dict[str, Any]] = field(
        default_factory=list
    )  # per call: node, model, leg, tokens
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
    return "429" in text or "rate limit reached" in text.lower()


def is_permanent_llm_error(text: str) -> bool:
    """Retrying cannot help: the prompt itself exceeds the model's request-size limit (HTTP 413)."""
    low = text.lower()
    return "413" in low or "request too large" in low or "context length" in low


class FullVariant:
    def __init__(self, graph: Graph, hook: EvalLLMHook, name: str = "full") -> None:
        self.name = name
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
                llm_calls=collector.calls,
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
            llm_calls=collector.calls,
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


def naive_system_prompt(ddl: str, evidence: str = "") -> str:
    system = NAIVE_SYSTEM.format(ddl=ddl)
    if evidence:
        system += NAIVE_HINT.format(evidence=evidence)
    return system


def naive_messages(ddl: str, question: str, evidence: str = "") -> list[dict[str, str]]:
    return [
        {"role": "system", "content": naive_system_prompt(ddl, evidence)},
        {"role": "user", "content": question},
    ]


class NaiveVariant:
    """B0. With ``small_cap`` set, the DDL is cut per question to fit the token cap."""

    def __init__(
        self,
        ddl_by_db: dict[str, str],
        hook: EvalLLMHook,
        *,
        name: str = "naive",
        profiles: dict[str, dict[str, Any]] | None = None,
        total_cap_tokens: int | None = None,
        max_output_tokens: int = 0,
    ) -> None:
        self.name = name
        self._ddl = ddl_by_db
        self._hook = hook
        self._profiles = profiles or {}
        self._cap = total_cap_tokens
        self._max_out = max_output_tokens

    def ddl_for(self, q: Question, use_evidence: bool) -> str | None:
        if self._cap is None:
            return self._ddl.get(q.db_id)
        profile = self._profiles.get(q.db_id)
        if profile is None:
            return None
        evidence = q.evidence if use_evidence else ""
        overhead = estimate_messages_tokens(naive_messages("", q.question, evidence))
        budget = self._cap - self._max_out - overhead
        return build_ddl_capped(profile, q.question, evidence, budget)

    async def predict(self, q: Question, use_evidence: bool) -> Prediction:
        ddl = self.ddl_for(q, use_evidence)
        if ddl is None:
            return Prediction(status="failed", error=f"no DDL for database {q.db_id!r}")
        system = naive_system_prompt(ddl, q.evidence if use_evidence else "")
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
                llm_calls=collector.calls,
            )
        finally:
            current_usage.reset(tok_u)
            llm_hook.reset(tok_h)
        return Prediction(
            status="answered" if sql else "failed",
            sql=sql,
            decision="sql" if sql else None,
            usage=collector.to_dict(),
            llm_calls=collector.calls,
            latency_s=time.perf_counter() - t0,
            error="" if sql else "empty model output",
        )


VariantFactory = Callable[[], Awaitable[Variant]]
