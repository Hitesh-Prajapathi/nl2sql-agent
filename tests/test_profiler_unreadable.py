"""The schema profiler must skip tables the agent role cannot SELECT from.

Uses a throwaway database and role created as the superuser (never touches
``nl2sql_assignment`` or any BIRD schema); both are dropped afterwards.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import asyncpg
import pytest
import pytest_asyncio
import structlog

from nl2sql.db.connection import create_pool
from nl2sql.schema.introspect import build_profile_data, introspect_schema
from nl2sql.schema.profile import DatabaseProfile
from nl2sql.schema.provider import FullSchemaProvider

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_SETUP_SQL = """
CREATE TABLE visible_t (id int PRIMARY KEY, label text);
INSERT INTO visible_t VALUES (1, 'a'), (2, 'b');
CREATE TABLE secret_t (id int, token text);
INSERT INTO secret_t VALUES (1, 'hunter2');
"""

ADMIN_BASE = os.environ.get("NL2SQL_TEST_ADMIN_BASE", "postgres:postgres@localhost:5432")


@pytest_asyncio.fixture
async def limited_pool() -> AsyncIterator[asyncpg.Pool]:
    """A pool for a role that can read ``visible_t`` but not ``secret_t``."""
    try:
        admin = await asyncpg.connect(f"postgresql://{ADMIN_BASE}/postgres", timeout=3)
    except Exception:
        pytest.skip("superuser connection unavailable")
    suffix = uuid.uuid4().hex[:8]
    db, role, pw = f"nl2sql_tmp_{suffix}", f"nl2sql_tmp_role_{suffix}", "tmp_pass"
    host = ADMIN_BASE.split("@", 1)[1]
    pool: asyncpg.Pool | None = None
    try:
        await admin.execute(f"CREATE ROLE {role} LOGIN PASSWORD '{pw}'")
        await admin.execute(f"CREATE DATABASE {db}")
        setup = await asyncpg.connect(f"postgresql://{ADMIN_BASE}/{db}")
        try:
            await setup.execute(_SETUP_SQL)
            await setup.execute(f"GRANT USAGE ON SCHEMA public TO {role}")
            await setup.execute(f"GRANT SELECT ON visible_t TO {role}")
        finally:
            await setup.close()
        pool = await create_pool(f"postgresql://{role}:{pw}@{host}/{db}", min_size=1, max_size=2)
        yield pool
    finally:
        if pool is not None:
            await pool.close()
        await admin.execute(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")
        await admin.execute(f"DROP ROLE IF EXISTS {role}")
        await admin.close()


async def test_unreadable_table_is_skipped_not_fatal(limited_pool: asyncpg.Pool) -> None:
    profile = DatabaseProfile(db_id="tmp", display_name="Tmp")
    data = await build_profile_data(limited_pool, profile)
    assert [t["name"] for t in data["tables"]] == ["visible_t"]

    context = await introspect_schema(limited_pool, profile)
    assert "TABLE: visible_t" in context
    assert "secret_t" not in context
    assert "hunter2" not in context


async def test_skipped_table_warned_once_and_hidden_from_provider(
    limited_pool: asyncpg.Pool,
) -> None:
    provider = FullSchemaProvider(limited_pool)
    with structlog.testing.capture_logs() as logs:
        context = await provider.get_context()
        await provider.get_context()  # cached: no second introspection
    warnings = [e for e in logs if e["event"] == "schema_table_skipped_unreadable"]
    assert [(w["table"], w["log_level"]) for w in warnings] == [("secret_t", "warning")]
    assert "secret_t" not in context
    assert await provider.table_names() == frozenset({"visible_t"})
