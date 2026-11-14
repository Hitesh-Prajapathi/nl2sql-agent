"""Prometheus metrics.

Single-process by default. With several uvicorn workers set ``PROMETHEUS_MULTIPROC_DIR``
(an empty, writable directory, wiped on deploy) in the environment *before* the process
starts; prometheus-client then writes per-process mmap files and ``/metrics`` aggregates
them through ``MultiProcessCollector``.
"""

from __future__ import annotations

import os

_MULTIPROC_DIR = os.environ.get("PROMETHEUS_MULTIPROC_DIR", "")
if _MULTIPROC_DIR:  # must exist before prometheus_client opens its value files
    os.makedirs(_MULTIPROC_DIR, exist_ok=True)

from prometheus_client import (  # noqa: E402
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    multiprocess,
)


def multiprocess_enabled() -> bool:
    return bool(os.environ.get("PROMETHEUS_MULTIPROC_DIR"))


def render_metrics() -> bytes:
    """Prometheus exposition text; aggregates all workers in multiprocess mode."""
    if multiprocess_enabled():
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)  # type: ignore[no-untyped-call]
        return generate_latest(registry)
    return generate_latest()


def mark_process_dead(pid: int) -> None:
    """Drop a dead worker's live-gauge files (call from a gunicorn child_exit hook)."""
    if multiprocess_enabled():
        multiprocess.mark_process_dead(pid)  # type: ignore[no-untyped-call]


REQUEST_LATENCY = Histogram(
    "nl2sql_request_latency_seconds",
    "End-to-end /query latency",
    ["route"],
    buckets=(0.1, 0.25, 0.5, 1, 2, 4, 8, 15, 30, 60),
)
REQUESTS = Counter("nl2sql_requests_total", "Requests by outcome", ["route", "status"])
BLOCKS = Counter("nl2sql_blocks_total", "Safety blocks by layer", ["layer"])
LLM_RETRIES = Counter("nl2sql_llm_retries_total", "LLM call retries", ["reason"])
LLM_FALLBACKS = Counter("nl2sql_llm_fallbacks_total", "LLM fallback-model activations")
LLM_BREAKER_OPEN = Gauge(
    "nl2sql_llm_breaker_open",
    "1 while the LLM circuit breaker is open (max across live workers)",
    multiprocess_mode="livemax",
)
TOKENS = Counter("nl2sql_llm_tokens_total", "LLM tokens", ["direction"])
COST = Counter("nl2sql_cost_usd_total", "Estimated LLM spend in USD")
NODE_LATENCY = Histogram(
    "nl2sql_node_latency_seconds",
    "Graph node latency",
    ["node"],
    buckets=(0.005, 0.02, 0.1, 0.5, 1, 2, 5, 15),
)
RATE_LIMITED = Counter("nl2sql_rate_limited_total", "Rejected by rate limiter", ["scope"])
