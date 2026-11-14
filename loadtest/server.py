"""Load-test app entrypoint: `uvicorn loadtest.server:app`.

Wraps the real FastAPI app, switches the fake LLM to canned SQL with lognormal latency,
and adds instrumentation routes under /_loadtest (pool size/idle, pool acquire waits,
event-loop lag). No source file under src/ is modified.

Env: LT_POOL_MAX (override asyncpg pool max_size, for tuning experiments).
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import asyncpg
import asyncpg.pool
from fastapi.routing import APIRoute

from loadtest import fake_script
from nl2sql.api.app import app
from nl2sql.db import connection
from nl2sql.llm.providers import get_fake_script

_seed = os.environ.get("LT_LATENCY_SEED")
fake_script.install(
    get_fake_script(),
    fake_script.LatencyParams.from_env(),
    int(_seed) if _seed else None,
)

# ── optional pool max_size override ──────────────────────────────────────────
_pool_max = os.environ.get("LT_POOL_MAX")
if _pool_max:
    _orig_create_pool = asyncpg.create_pool

    def _create_pool(*args: Any, **kwargs: Any) -> Any:
        kwargs["max_size"] = int(_pool_max)
        return _orig_create_pool(*args, **kwargs)

    asyncpg.create_pool = _create_pool

# ── instrumentation ──────────────────────────────────────────────────────────
_waits_ms: list[float] = []
_lags_ms: list[float] = []
_lag_task: asyncio.Task[None] | None = None

_orig_aenter = asyncpg.pool.PoolAcquireContext.__aenter__


async def _timed_aenter(self: Any) -> Any:
    t0 = time.perf_counter()
    try:
        return await _orig_aenter(self)
    finally:
        _waits_ms.append((time.perf_counter() - t0) * 1000)


setattr(asyncpg.pool.PoolAcquireContext, "__aenter__", _timed_aenter)


async def _lag_monitor() -> None:
    interval = 0.05
    while True:
        t0 = time.perf_counter()
        await asyncio.sleep(interval)
        _lags_ms.append(max(0.0, (time.perf_counter() - t0 - interval) * 1000))


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    return s[min(len(s) - 1, int(len(s) * q))]


def _summ(values: list[float]) -> dict[str, float]:
    return {
        "n": float(len(values)),
        "mean": sum(values) / len(values) if values else 0.0,
        "p95": _pct(values, 0.95),
        "p99": _pct(values, 0.99),
        "max": max(values) if values else 0.0,
    }


async def _stats() -> dict[str, Any]:
    global _lag_task
    if _lag_task is None:
        _lag_task = asyncio.create_task(_lag_monitor())
    pool = connection._pool
    return {
        "pool": (
            {
                "size": pool.get_size(),
                "idle": pool.get_idle_size(),
                "max": pool.get_max_size(),
            }
            if pool
            else None
        ),
        "acquire_wait_ms": _summ(_waits_ms),
        "loop_lag_ms": _summ(_lags_ms),
    }


async def _reset() -> dict[str, str]:
    await _stats()
    _waits_ms.clear()
    _lags_ms.clear()
    return {"status": "reset"}


app.router.routes.insert(0, APIRoute("/_loadtest/stats", _stats, methods=["GET"]))
app.router.routes.insert(0, APIRoute("/_loadtest/reset", _reset, methods=["POST"]))
