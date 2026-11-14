"""OpenTelemetry setup, plus a LangChain callback that records usage and per-node spans."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from nl2sql.config import get_settings
from nl2sql.telemetry import metrics

_provider: TracerProvider | None = None

GRAPH_NODES = frozenset(
    {
        "load_schema",
        "plan_sql",
        "clarify",
        "refuse",
        "validate_sql",
        "max_retries",
        "execute_sql",
        "correct_sql",
        "synthesize",
    }
)


def setup_tracing() -> TracerProvider:
    """Install the tracer provider once. OTLP export only when an endpoint is set."""
    global _provider
    if _provider is not None:
        return _provider
    s = get_settings()
    provider = TracerProvider(resource=Resource.create({"service.name": s.otel_service_name}))
    if s.otel_exporter_otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        endpoint = s.otel_exporter_otlp_endpoint.rstrip("/") + "/v1/traces"
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)
    _provider = provider
    return provider


def get_tracer() -> trace.Tracer:
    return setup_tracing().get_tracer("nl2sql")


def current_trace_id() -> str | None:
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else None


def shutdown_tracing() -> None:
    if _provider is not None:
        _provider.shutdown()


def compute_cost(input_tokens: int, output_tokens: int) -> float | None:
    """USD cost from settings prices; None when prices are not configured."""
    s = get_settings()
    if s.price_input_per_1m_usd is None or s.price_output_per_1m_usd is None:
        return None
    return (
        input_tokens * s.price_input_per_1m_usd + output_tokens * s.price_output_per_1m_usd
    ) / 1_000_000


class RequestObserver(BaseCallbackHandler):
    """Per-request callback: token usage, per-node spans/latency and progress events.

    Attached via ``config["callbacks"]`` from the API layer; no agent code changes.
    """

    def __init__(self, on_node: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.input_tokens = 0
        self.output_tokens = 0
        self.llm_calls = 0
        self.has_usage = False
        self._spans: dict[UUID, tuple[trace.Span, float, str]] = {}
        self._on_node = on_node

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        self.llm_calls += 1
        for gens in response.generations:
            for g in gens:
                um = getattr(getattr(g, "message", None), "usage_metadata", None)
                if um:
                    self._add(int(um.get("input_tokens", 0)), int(um.get("output_tokens", 0)))
                    return
        usage = (response.llm_output or {}).get("token_usage") or {}
        if usage:
            self._add(int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0)))

    def _add(self, i: int, o: int) -> None:
        self.has_usage = True
        self.input_tokens += i
        self.output_tokens += o
        metrics.TOKENS.labels("input").inc(i)
        metrics.TOKENS.labels("output").inc(o)

    @property
    def cost_usd(self) -> float | None:
        if not self.has_usage:
            return None
        return compute_cost(self.input_tokens, self.output_tokens)

    def on_chain_start(
        self,
        serialized: dict[str, Any] | None,
        inputs: Any,
        *,
        run_id: UUID,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        node = (metadata or {}).get("langgraph_node")
        if not node or kwargs.get("name") != node or node not in GRAPH_NODES:
            return
        span = get_tracer().start_span(f"node.{node}")
        self._spans[run_id] = (span, time.perf_counter(), node)
        if self._on_node:
            self._on_node({"event": "node_start", "node": node})

    def _end(self, run_id: UUID, error: BaseException | None = None) -> None:
        item = self._spans.pop(run_id, None)
        if item is None:
            return
        span, t0, node = item
        dt = time.perf_counter() - t0
        metrics.NODE_LATENCY.labels(node).observe(dt)
        if error is not None:
            span.record_exception(error)
        span.end()
        if self._on_node:
            self._on_node({"event": "node_end", "node": node, "ms": round(dt * 1000, 1)})

    def on_chain_end(self, outputs: Any, *, run_id: UUID, **kwargs: Any) -> None:
        self._end(run_id)

    def on_chain_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        self._end(run_id, error)
