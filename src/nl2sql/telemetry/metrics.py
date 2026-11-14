"""Prometheus metrics. Single-process registry (multi-worker needs multiprocess mode)."""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

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
LLM_BREAKER_OPEN = Gauge("nl2sql_llm_breaker_open", "1 while the LLM circuit breaker is open")
TOKENS = Counter("nl2sql_llm_tokens_total", "LLM tokens", ["direction"])
COST = Counter("nl2sql_cost_usd_total", "Estimated LLM spend in USD")
NODE_LATENCY = Histogram(
    "nl2sql_node_latency_seconds",
    "Graph node latency",
    ["node"],
    buckets=(0.005, 0.02, 0.1, 0.5, 1, 2, 5, 15),
)
RATE_LIMITED = Counter("nl2sql_rate_limited_total", "Rejected by rate limiter", ["scope"])
