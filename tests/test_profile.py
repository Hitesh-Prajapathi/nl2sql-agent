from __future__ import annotations

import re
import socket

import pytest

from nl2sql.agent.nodes.plan import refusal_text
from nl2sql.agent.nodes.synthesize import _empty_answer
from nl2sql.llm.prompts.plan_sql import PLAN_SQL_SYSTEM
from nl2sql.schema.profile import DatabaseProfile


def _pg_up() -> bool:
    try:
        with socket.create_connection(("localhost", 5432), timeout=1):
            return True
    except OSError:
        return False


requires_pg = pytest.mark.skipif(not _pg_up(), reason="Postgres unreachable")


# ── Unit ──────────────────────────────────────────────────────────────────────

def test_header_plain_and_with_domain() -> None:
    p = DatabaseProfile(db_id="x", display_name="Acme DB")
    assert p.header().startswith("=== DATABASE SCHEMA (Acme DB) ===")
    assert "Domain:" not in p.header()
    p.domain_description = "Retail sales"
    assert "Domain: Retail sales" in p.header()
    assert "E-COMMERCE" not in p.header()


def test_refusal_text_generic() -> None:
    assert "Acme DB" in refusal_text("Acme DB")
    assert "e-commerce" not in refusal_text("Acme DB").lower()
    assert "connected database" in refusal_text("")


def test_empty_answer_with_ranges() -> None:
    out = _empty_answer("orders last month?", {"orders.order_date": ("2024-01-01", "2024-02-01")})
    assert "2024-01-01" in out and "2024-02-01" in out and "orders.order_date" in out


def test_empty_answer_without_ranges() -> None:
    out = _empty_answer("orders last month?", {})
    assert out == "No rows matched your query."
    assert _empty_answer("orders last month?") == out
    assert "2026" not in out


def test_empty_answer_non_temporal() -> None:
    out = _empty_answer("list gold customers", {"a.b": ("2024-01-01", "2024-02-01")})
    assert out == "No rows matched your query."


def test_prompt_has_no_real_data() -> None:
    assert "Rahul" not in PLAN_SQL_SYSTEM
    assert "Menon" not in PLAN_SQL_SYSTEM
    emails = re.findall(r"[\w.]+@[\w.]+", PLAN_SQL_SYSTEM)
    assert all(e.endswith(".invalid") for e in emails)


# ── Integration ───────────────────────────────────────────────────────────────

@pytest.mark.integration
@requires_pg
@pytest.mark.asyncio
async def test_provider_derives_profile_and_dates() -> None:
    from nl2sql.db.connection import close_pool, get_pool
    from nl2sql.schema.provider import FullSchemaProvider

    pool = await get_pool()
    try:
        provider = FullSchemaProvider(pool)
        ctx = await provider.get_context("x")
        async with pool.acquire() as conn:
            name = await conn.fetchval("SELECT current_database()")
            row = await conn.fetchrow("SELECT MIN(order_date) mn, MAX(order_date) mx FROM orders")
        prof = provider.profile
        assert prof is not None
        assert prof.db_id == name and prof.display_name == name
        assert prof.date_ranges["orders.order_date"] == (str(row["mn"]), str(row["mx"]))
        assert "E-COMMERCE PLATFORM" not in ctx
        assert ctx.startswith(f"=== DATABASE SCHEMA ({name}) ===")
    finally:
        await close_pool()
