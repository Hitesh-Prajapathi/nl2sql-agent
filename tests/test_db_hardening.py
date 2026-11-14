"""Integration tests for db/hardening.sql, connecting as the read-only role.

Requires hardening to have been applied (scripts/apply_db_hardening.sh).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import asyncpg
import pytest
import pytest_asyncio

from nl2sql.config import get_settings

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

REVOKED = [
    "SELECT query_to_xml('select 1', true, true, '')",
    "SELECT table_to_xml('customers'::regclass, true, true, '')",
    "SELECT pg_sleep(0.01)",
    "SELECT pg_advisory_lock(1)",
    "SELECT pg_try_advisory_xact_lock(1)",
    "SELECT set_config('statement_timeout', '0', true)",
    "SELECT current_setting('server_version')",
    "SELECT txid_current()",
    "SELECT pg_current_wal_lsn()",
    "SELECT pg_terminate_backend(999999)",
    "SELECT pg_notify('c', 'p')",
    "SELECT version()",
    "SELECT inet_server_addr()",
    "SELECT pg_ls_dir('.')",
    "SELECT pg_read_file('postgresql.conf')",
    "SELECT lo_creat(-1)",
    "SELECT pg_database_size('nl2sql_assignment')",
    "SELECT * FROM pg_roles",
    "SELECT * FROM pg_user",
    "SELECT * FROM pg_settings",
    "SELECT * FROM pg_stat_activity",
    "SELECT * FROM information_schema.routines",
    "DO LANGUAGE plpgsql $$ BEGIN NULL; END $$",
]


@pytest_asyncio.fixture
async def conn() -> AsyncIterator[asyncpg.Connection]:
    c = await asyncpg.connect(get_settings().database_url)
    try:
        yield c
    finally:
        await c.close()


@pytest.mark.parametrize("sql", REVOKED)
async def test_revoked_is_permission_denied(conn: asyncpg.Connection, sql: str) -> None:
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await conn.fetch(sql)


async def test_analytical_select_works(conn: asyncpg.Connection) -> None:
    rows = await conn.fetch(
        "SELECT status, count(*) AS n, round(avg(total_amount), 2) AS avg_total "
        "FROM orders GROUP BY status ORDER BY n DESC"
    )
    assert rows


async def test_schema_introspection_works(conn: asyncpg.Connection) -> None:
    tables = await conn.fetch(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"
    )
    assert {r["table_name"] for r in tables} >= {"customers", "orders"}
    cols = await conn.fetch(
        "SELECT column_name FROM information_schema.columns WHERE table_name='orders'"
    )
    assert cols
    assert await conn.fetchval("SELECT current_database()") == "nl2sql_assignment"


async def test_search_path_pinned(conn: asyncpg.Connection) -> None:
    # SHOW is a utility statement, not a function, so it still works.
    assert await conn.fetchval("SHOW search_path") == "public, pg_catalog"


async def test_pool_acquire_release_roundtrip() -> None:
    """asyncpg's reset query calls pg_advisory_unlock_all() on every release."""
    pool = await asyncpg.create_pool(get_settings().database_url, min_size=1, max_size=2)
    try:
        for _ in range(3):
            async with pool.acquire() as c:
                assert await c.fetchval("SELECT 1") == 1
        async with pool.acquire() as c:
            assert await c.fetchval("SELECT count(*) FROM customers") >= 0
    finally:
        await pool.close()
