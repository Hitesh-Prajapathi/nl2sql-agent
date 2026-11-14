"""Integration tests against the BIRD Mini-Dev database (skipped when it is absent)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
import pytest

from nl2sql.config import get_settings
from nl2sql.db.executor import execute_sql
from nl2sql.db.registry import DatabaseRegistry
from nl2sql.eval.dataset import QUESTIONS_REL, Question, load_questions
from nl2sql.eval.metrics import execution_match
from nl2sql.eval.sqlrunner import RegistrySqlRunner
from nl2sql.safety.validator import validate_sql
from nl2sql.schema.introspect import build_profile_data, render_context
from nl2sql.schema.profile import DatabaseProfile

DATA_DIR = Path(get_settings().bird_data_dir)


def _bird_available() -> bool:
    if not (DATA_DIR / QUESTIONS_REL).exists():
        return False
    dsn = get_settings().bird_database_url_template.format(db_id="superhero")

    async def probe() -> None:
        conn = await asyncpg.connect(dsn, timeout=3)
        await conn.close()

    try:
        asyncio.run(probe())
    except Exception:
        return False
    return True


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _bird_available(), reason="BIRD Mini-Dev database not set up"),
]


@pytest.fixture
async def registry() -> AsyncIterator[DatabaseRegistry]:
    reg = DatabaseRegistry.for_bird(
        sorted({q.db_id for q in load_questions(DATA_DIR)}), use_stored_profiles=False
    )
    try:
        yield reg
    finally:
        await reg.close()


async def test_layout_is_schema_per_db_id_with_stats(registry: DatabaseRegistry) -> None:
    admin = await asyncpg.connect(
        get_settings().bird_database_url_template.format(db_id="superhero")
    )
    try:
        rows = await admin.fetch(
            "SELECT n.nspname, count(*) AS n FROM pg_class c JOIN pg_namespace n "
            "ON n.oid = c.relnamespace WHERE c.relkind = 'r' AND n.nspname NOT LIKE 'pg\\_%' "
            "AND n.nspname <> 'information_schema' GROUP BY 1"
        )
        counts = {r["nspname"]: r["n"] for r in rows}
        assert "public" not in counts, "no table may be left in public"
        assert len(counts) == 11 and sum(counts.values()) == 75
        # ANALYZE ran: reltuples is populated for the schema this role can see
        reltuples = await admin.fetchval(
            "SELECT min(c.reltuples) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname = 'superhero' AND c.relkind = 'r'"
        )
        assert reltuples > 0
    finally:
        await admin.close()


async def test_reader_role_is_isolated_and_read_only(registry: DatabaseRegistry) -> None:
    pool = await registry.pool_for("superhero")
    ok = await execute_sql(pool, "SELECT COUNT(*) AS n FROM superhero")
    assert ok.success and ok.rows[0]["n"] > 0
    other = await execute_sql(pool, "SELECT COUNT(*) FROM formula_1.drivers")
    assert not other.success and "permission denied" in (other.error or "")
    unqualified = await execute_sql(pool, "SELECT COUNT(*) FROM drivers")
    assert not unqualified.success  # other databases' tables are not even on the search_path
    write = await execute_sql(pool, "CREATE TABLE superhero.x (a int)")
    assert not write.success
    ins = await execute_sql(pool, "DELETE FROM superhero")
    assert not ins.success


async def test_gold_sql_runs_and_passes_validator_for_every_question(
    registry: DatabaseRegistry,
) -> None:
    questions = load_questions(DATA_DIR)
    assert len(questions) == 500
    runner = RegistrySqlRunner(registry)
    sem = asyncio.Semaphore(4)

    async def one(q: Question) -> tuple[bool, bool]:
        async with sem:
            out = await runner.run(q.db_id, q.gold_sql)
        return validate_sql(q.gold_sql).is_valid, out.ok

    results = await asyncio.gather(*(one(q) for q in questions))
    assert [i for i, (valid, _) in enumerate(results) if not valid] == []  # no false blocks
    assert [i for i, (_, ok) in enumerate(results) if not ok] == []  # all gold SQL executes


async def test_eval_executor_keeps_duplicate_columns_and_is_uncapped(
    registry: DatabaseRegistry,
) -> None:
    runner = RegistrySqlRunner(registry)
    dup = await runner.run("superhero", "SELECT a.id, b.id FROM alignment a, alignment b LIMIT 3")
    assert dup.ok and dup.rows is not None and len(dup.rows[0]) == 2
    big = await runner.run("financial", "SELECT trans_id FROM trans")
    assert big.ok and big.rows is not None and len(big.rows) > 100_000  # no row cap
    bad = await runner.run("superhero", "SELECT nope FROM superhero")
    assert not bad.ok and "UndefinedColumn" in (bad.error or "")


async def test_ex_between_equivalent_gold_variants(registry: DatabaseRegistry) -> None:
    runner = RegistrySqlRunner(registry)
    a = await runner.run("superhero", "SELECT id FROM alignment ORDER BY id")
    b = await runner.run("superhero", "SELECT id FROM alignment ORDER BY id DESC")
    assert a.rows is not None and b.rows is not None and execution_match(a.rows, b.rows)


async def test_profile_uses_catalog_stats_and_scales(registry: DatabaseRegistry) -> None:
    pool = await registry.pool_for("european_football_2")
    profile = DatabaseProfile(db_id="european_football_2", display_name="european_football_2")
    data = await build_profile_data(pool, profile)
    tables = {t["name"]: t for t in data["tables"]}
    pa = tables["player_attributes"]
    assert pa["rows_estimated"] and pa["rows"] > 100_000  # from reltuples, not COUNT(*)
    assert pa["primary_key"] == ["id"]
    assert any(c.get("values") for c in pa["columns"])  # pg_stats most_common_vals
    text = render_context(data, 1500)
    assert text.startswith("=== DATABASE SCHEMA (european_football_2)")
    assert "~" in text and "Sample rows" in text
