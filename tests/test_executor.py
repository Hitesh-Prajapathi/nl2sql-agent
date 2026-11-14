from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest

from nl2sql.config import get_settings
from nl2sql.db import executor
from nl2sql.db.executor import execute_sql
from nl2sql.models import ExecutionResult


def make_pool(
    rows: list[dict[str, Any]],
    cols: tuple[str, ...] = ("id", "name"),
    prepare_exc: Exception | None = None,
    fetch_exc: Exception | None = None,
) -> Any:
    """Mock pool -> conn -> prepared stmt -> cursor chain."""
    remaining = list(rows)
    requested: list[int] = []

    async def fetch(n: int) -> list[dict[str, Any]]:
        requested.append(n)
        if fetch_exc:
            raise fetch_exc
        out, remaining[:] = remaining[:n], remaining[n:]
        return out

    cursor = SimpleNamespace(fetch=fetch)
    stmt = MagicMock()
    stmt.get_attributes.return_value = [SimpleNamespace(name=c) for c in cols]
    stmt.cursor = AsyncMock(return_value=cursor)

    conn = MagicMock()
    conn.prepare = AsyncMock(return_value=stmt, side_effect=prepare_exc)
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=tx)

    acq = MagicMock()
    acq.__aenter__ = AsyncMock(return_value=conn)
    acq.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acq)
    pool.requested = requested
    pool.conn = conn
    return pool


def mk_rows(n: int) -> list[dict[str, Any]]:
    return [{"id": i, "name": f"n{i}"} for i in range(n)]


async def test_under_cap() -> None:
    pool = make_pool(mk_rows(3))
    r = await execute_sql(pool, "select 1", row_cap=5)
    assert r.success and r.row_count == 3 and not r.truncated
    assert r.columns == ["id", "name"]
    pool.conn.transaction.assert_called_once_with(readonly=True)
    assert pool.requested == [6]


async def test_exactly_at_cap() -> None:
    r = await execute_sql(make_pool(mk_rows(5)), "q", row_cap=5)
    assert r.row_count == 5 and not r.truncated


async def test_over_cap() -> None:
    r = await execute_sql(make_pool(mk_rows(6)), "q", row_cap=5)
    assert r.row_count == 5 and r.truncated
    assert r.rows[-1]["id"] == 4


async def test_default_cap_is_row_cap() -> None:
    pool = make_pool(mk_rows(1))
    await execute_sql(pool, "q")
    assert pool.requested == [executor.ROW_CAP + 1]


async def test_row_cap_none_fetches_everything() -> None:
    pool = make_pool(mk_rows(2500))
    r = await execute_sql(pool, "q", row_cap=None)
    assert r.row_count == 2500 and not r.truncated


async def test_empty_result_keeps_columns() -> None:
    r = await execute_sql(make_pool([], cols=("a", "b")), "q", row_cap=5)
    assert r.success and r.rows == [] and r.columns == ["a", "b"]
    assert not r.truncated


async def test_byte_cap_truncates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(executor, "MAX_RESULT_BYTES", 100)
    rows = [{"id": i, "name": "x" * 20} for i in range(50)]
    r = await execute_sql(make_pool(rows), "q", row_cap=None)
    assert r.truncated and 0 < r.row_count < 50


async def test_byte_cap_not_hit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(executor, "MAX_RESULT_BYTES", 10_000)
    r = await execute_sql(make_pool(mk_rows(5)), "q", row_cap=10)
    assert not r.truncated and r.row_count == 5


@pytest.mark.parametrize(
    "exc, check",
    [
        (asyncpg.exceptions.ReadOnlySQLTransactionError("ro"), lambda r: r.is_safety_block),
        (asyncpg.exceptions.QueryCanceledError("t"), lambda r: "statement_timeout" in r.error),
        (
            asyncpg.exceptions.UndefinedTableError("t"),
            lambda r: r.error.startswith("Table not found"),
        ),
        (
            asyncpg.exceptions.UndefinedColumnError("c"),
            lambda r: r.error.startswith("Column not found"),
        ),
        (asyncpg.exceptions.SyntaxOrAccessError("s"), lambda r: r.error and not r.is_safety_block),
        (RuntimeError("boom"), lambda r: r.error.startswith("Unexpected error")),
    ],
)
@pytest.mark.parametrize("where", ["prepare", "fetch"])
async def test_exception_mapping(
    exc: Exception, check: Callable[[ExecutionResult], Any], where: str
) -> None:
    kw: dict[str, Any] = {"prepare_exc": exc} if where == "prepare" else {"fetch_exc": exc}
    r = await execute_sql(make_pool([], **kw), "q")
    assert r.success is False and check(r)


# ---------------------------------------------------------------- integration

DSN = get_settings().database_url


async def _reachable() -> bool:
    try:
        c = await asyncio.wait_for(asyncpg.connect(DSN), 3)
        await c.close()
        return True
    except Exception:
        return False


def _pg_up() -> bool:
    return asyncio.run(_reachable())


needs_pg = pytest.mark.skipif(not _pg_up(), reason="Postgres not reachable")


@pytest.fixture
async def real_pool() -> AsyncIterator[asyncpg.Pool]:
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
    try:
        yield pool
    finally:
        await pool.close()


@pytest.mark.integration
@needs_pg
async def test_int_large_result_truncated(real_pool: asyncpg.Pool) -> None:
    r = await execute_sql(real_pool, "SELECT * FROM generate_series(1, 100000)", row_cap=500)
    assert r.success and r.row_count == 500 and r.truncated


@pytest.mark.integration
@needs_pg
async def test_int_empty_result_has_columns(real_pool: asyncpg.Pool) -> None:
    r = await execute_sql(real_pool, "SELECT 1 AS a, 'x'::text AS b WHERE false")
    assert r.success and r.rows == [] and r.columns == ["a", "b"]


@pytest.mark.integration
@needs_pg
async def test_int_write_blocked(real_pool: asyncpg.Pool) -> None:
    before = await real_pool.fetchval("SELECT count(*) FROM customers")
    r = await execute_sql(real_pool, "DELETE FROM customers")
    assert r.success is False and r.is_safety_block
    assert await real_pool.fetchval("SELECT count(*) FROM customers") == before


@pytest.mark.integration
@needs_pg
async def test_int_statement_timeout() -> None:
    pool = await asyncpg.create_pool(
        DSN, min_size=1, max_size=1, server_settings={"statement_timeout": "200"}
    )
    try:
        r = await execute_sql(
            pool, "SELECT count(*) FROM generate_series(1, 400000) a, generate_series(1, 4000) b"
        )
        assert r.success is False and r.error is not None and "statement_timeout" in r.error
    finally:
        await pool.close()
