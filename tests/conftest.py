from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator, Iterator
from typing import Any
from urllib.parse import urlparse

import asyncpg
import pytest

import nl2sql.agent.graph as graph_mod
import nl2sql.agent.nodes.execute as execute_mod
from nl2sql.config import get_settings
from nl2sql.db import connection
from nl2sql.llm import providers as providers_mod
from nl2sql.llm.providers import FakeLLMScript, reset_fake_script
from tests.fakes import SetSettings

# ── Postgres availability (integration tests) ────────────────────────────────

_pg_ok: bool | None = None


def _postgres_reachable() -> bool:
    global _pg_ok
    if _pg_ok is not None:
        return _pg_ok
    url = get_settings().database_url
    u = urlparse(url)
    try:
        with socket.create_connection((u.hostname or "localhost", u.port or 5432), timeout=1):
            pass
        import asyncpg

        async def _probe() -> None:
            conn = await asyncpg.connect(url, timeout=3)
            await conn.close()

        asyncio.run(_probe())
        _pg_ok = True
    except Exception:
        _pg_ok = False
    return _pg_ok


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    integ = [i for i in items if "integration" in i.keywords]
    if not integ or _postgres_reachable():
        return
    skip = pytest.mark.skip(reason="Postgres is not reachable; integration test skipped")
    for item in integ:
        item.add_marker(skip)


# ── Settings / fake LLM fixtures ─────────────────────────────────────────────


@pytest.fixture
def set_settings(monkeypatch: pytest.MonkeyPatch) -> SetSettings:
    """set_settings(max_retry_attempts=1, ...) patches every module-level `settings`."""

    def _set(**kw: Any) -> None:
        for mod in (graph_mod, execute_mod, providers_mod):
            for k, v in kw.items():
                monkeypatch.setattr(mod.settings, k, v)

    return _set


@pytest.fixture
def fake_llm(set_settings: SetSettings) -> Iterator[FakeLLMScript]:
    """Select the fake provider (no network) and return its fresh script."""
    set_settings(llm_provider="fake", fake_llm_latency_ms=0.0, max_retry_attempts=3)
    script = reset_fake_script()
    yield script
    script.reset()


# ── Real pool (integration) ──────────────────────────────────────────────────


@pytest.fixture
async def pg_pool() -> AsyncIterator[asyncpg.Pool]:
    """Real read-only pool; closed at teardown (the pool is a module global bound
    to the event loop, so it must not leak into the next test's loop)."""
    pool = await connection.get_pool()
    try:
        yield pool
    finally:
        await connection.close_pool()


@pytest.fixture(autouse=True)
def _drop_stale_pool() -> Iterator[None]:
    yield
    # Safety net: never let a pool from a finished event loop leak across tests.
    connection._pool = None
