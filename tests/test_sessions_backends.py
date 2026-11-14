"""Behavioural suite for every SessionStore backend (memory, postgres, redis).

Postgres/Redis are integration tests, skipped when unreachable. Redis URL comes from
``NL2SQL_TEST_REDIS_URL`` (default redis://localhost:6379/15).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from nl2sql.api.sessions import (
    InMemorySessionStore,
    SessionStore,
    build_session_store,
)
from nl2sql.config import Settings, get_settings

Factory = Callable[..., SessionStore]

integration = pytest.mark.integration
BACKENDS = [
    "memory",
    pytest.param("postgres", marks=integration),
    pytest.param("redis", marks=integration),
]
SHARED = [pytest.param("postgres", marks=integration), pytest.param("redis", marks=integration)]
REDIS_URL = os.environ.get("NL2SQL_TEST_REDIS_URL", "redis://localhost:6379/15")
SQL_FILE = Path(__file__).resolve().parents[1] / "db" / "app_state.sql"


async def _ensure_pg_schema() -> None:
    try:
        conn = await asyncpg.connect(get_settings().database_admin_url, timeout=3)
    except Exception:
        pytest.skip("Postgres admin connection unavailable")
    try:
        await conn.execute(SQL_FILE.read_text())
    finally:
        await conn.close()


async def _redis_up() -> bool:
    pytest.importorskip("redis")
    from redis.asyncio import Redis

    client = Redis.from_url(REDIS_URL)
    try:
        await client.ping()
        return True
    except Exception:
        return False
    finally:
        await client.aclose()


@pytest.fixture
async def factory(request: pytest.FixtureRequest) -> AsyncIterator[Factory]:
    backend: str = request.param
    created: list[Any] = []
    prefix = f"test:{uuid.uuid4().hex}:"
    if backend == "postgres":
        await _ensure_pg_schema()
    elif backend == "redis" and not await _redis_up():
        pytest.skip("Redis is not reachable")

    def make(ttl: float = 60, max_turns: int = 20, max_bytes: int = 65536) -> SessionStore:
        if backend == "memory":
            return InMemorySessionStore(ttl, max_turns, max_bytes, 100)
        cfg = Settings(
            session_backend=backend,
            session_ttl_seconds=int(ttl) if ttl >= 1 else 1,
            session_max_turns=max_turns,
            session_max_bytes=max_bytes,
            session_redis_url=REDIS_URL,
            session_redis_prefix=prefix,
        )
        store = build_session_store(cfg)
        created.append(store)
        return store

    yield make
    for st in created:
        await st.close()


def sid() -> str:
    return f"s-{uuid.uuid4().hex}"


@pytest.mark.parametrize("factory", BACKENDS, indirect=True)
async def test_roundtrip_and_isolation(factory: Factory) -> None:
    st = factory()
    a, b = sid(), sid()
    assert await st.get(a) == []
    await st.append(a, {"q": "one", "n": 1})
    await st.append(a, {"q": "two", "n": 2})
    await st.append(b, {"q": "other"})
    assert await st.get(a) == [{"q": "one", "n": 1}, {"q": "two", "n": 2}]
    assert await st.get(b) == [{"q": "other"}]


@pytest.mark.parametrize("factory", BACKENDS, indirect=True)
async def test_get_returns_copy(factory: Factory) -> None:
    st = factory()
    s = sid()
    await st.append(s, {"i": 0})
    (await st.get(s)).append({"i": "junk"})
    assert await st.get(s) == [{"i": 0}]


@pytest.mark.parametrize("factory", BACKENDS, indirect=True)
async def test_max_turns_keeps_latest(factory: Factory) -> None:
    st = factory(max_turns=3)
    s = sid()
    for i in range(7):
        await st.append(s, {"i": i})
    assert [t["i"] for t in await st.get(s)] == [4, 5, 6]


@pytest.mark.parametrize("factory", BACKENDS, indirect=True)
async def test_max_turns_zero_stores_nothing(factory: Factory) -> None:
    st = factory(max_turns=0)
    s = sid()
    await st.append(s, {"i": 1})
    assert await st.get(s) == []


@pytest.mark.parametrize("factory", BACKENDS, indirect=True)
async def test_max_bytes_evicts_oldest(factory: Factory) -> None:
    st = factory(max_bytes=120)
    s = sid()
    for i in range(6):
        await st.append(s, {"i": i, "pad": "x" * 30})
    got = await st.get(s)
    assert 1 <= len(got) < 6
    assert got[-1]["i"] == 5
    assert [t["i"] for t in got] == sorted(t["i"] for t in got)


@pytest.mark.parametrize("factory", BACKENDS, indirect=True)
async def test_oversized_single_turn_dropped(factory: Factory) -> None:
    st = factory(max_bytes=50)
    s = sid()
    await st.append(s, {"pad": "x" * 500})
    assert await st.get(s) == []


@pytest.mark.parametrize("factory", BACKENDS, indirect=True)
async def test_byte_budget_matches_memory_store(factory: Factory) -> None:
    """Every backend trims to exactly the same set as the reference in-memory store."""
    ref = InMemorySessionStore(60, 20, 300, 100)
    st = factory(max_bytes=300)
    s = sid()
    for i in range(12):
        turn = {"i": i, "pad": "é" * (i % 5 + 3)}
        await ref.append("r", turn)
        await st.append(s, turn)
    assert await st.get(s) == await ref.get("r")


@pytest.mark.parametrize("factory", BACKENDS, indirect=True)
async def test_ttl_expiry(factory: Factory) -> None:
    st = factory(ttl=1)
    s = sid()
    await st.append(s, {"i": 1})
    assert await st.get(s) == [{"i": 1}]
    await asyncio.sleep(1.3)
    assert await st.get(s) == []
    await st.append(s, {"i": 2})  # an expired thread restarts, old turns are not revived
    assert await st.get(s) == [{"i": 2}]


@pytest.mark.parametrize("factory", BACKENDS, indirect=True)
async def test_concurrent_appends_one_thread(factory: Factory) -> None:
    st = factory(max_turns=1000, max_bytes=10_000_000)
    s = sid()
    n = 60
    await asyncio.gather(*(st.append(s, {"i": i}) for i in range(n)))
    got = await st.get(s)
    assert len(got) == n
    assert sorted(t["i"] for t in got) == list(range(n))  # none lost or duplicated


@pytest.mark.parametrize("factory", BACKENDS, indirect=True)
async def test_concurrent_appends_respect_max_turns(factory: Factory) -> None:
    st = factory(max_turns=10)
    s = sid()
    await asyncio.gather(*(st.append(s, {"i": i}) for i in range(40)))
    assert len(await st.get(s)) == 10


@pytest.mark.parametrize("factory", BACKENDS, indirect=True)
async def test_sequential_ordering_preserved(factory: Factory) -> None:
    st = factory()
    s = sid()
    for i in range(15):
        await st.append(s, {"i": i})
    assert [t["i"] for t in await st.get(s)] == list(range(15))


@pytest.mark.parametrize("factory", SHARED, indirect=True)
async def test_cross_instance_shared_state(factory: Factory) -> None:
    one, two = factory(), factory()
    s = sid()
    await one.append(s, {"who": "one"})
    await two.append(s, {"who": "two"})
    assert [t["who"] for t in await one.get(s)] == ["one", "two"]
    assert await two.get(s) == await one.get(s)


@pytest.mark.parametrize("factory", SHARED, indirect=True)
async def test_cross_instance_concurrent_appends(factory: Factory) -> None:
    stores = [factory(max_turns=1000, max_bytes=10_000_000) for _ in range(4)]
    s = sid()
    await asyncio.gather(*(stores[i % 4].append(s, {"i": i}) for i in range(80)))
    got = await stores[0].get(s)
    assert sorted(t["i"] for t in got) == list(range(80))


def test_factory_default_is_memory_and_rejects_unknown() -> None:
    assert isinstance(build_session_store(Settings()), InMemorySessionStore)
    with pytest.raises(ValueError):
        build_session_store(Settings(session_backend="mongo"))
