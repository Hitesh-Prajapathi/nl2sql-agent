"""Deterministic SQL-level red-team suite (no LLM).

Every attack in redteam/sql_payloads.yaml must be blocked by ``validate_sql``; every query in
redteam/sql_benign.yaml must pass it (and, as an integration test, execute on a real Postgres).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import psycopg
import pytest
import yaml

from nl2sql.safety.allowlist import ALLOWED_FUNCTIONS
from nl2sql.safety.validator import validate_sql

REDTEAM = Path(__file__).resolve().parent.parent / "redteam"
PAYLOADS: list[dict[str, Any]] = yaml.safe_load((REDTEAM / "sql_payloads.yaml").read_text())
BENIGN: list[dict[str, Any]] = yaml.safe_load((REDTEAM / "sql_benign.yaml").read_text())
DSN = os.environ.get(
    "NL2SQL_REDTEAM_DSN",
    "postgresql://nl2sql_reader:reader_pass@localhost:5432/nl2sql_assignment",
)


@pytest.fixture(autouse=True)
def _quiet_sqlglot() -> None:
    logging.getLogger("sqlglot").setLevel(logging.ERROR)


def _postgres_reachable() -> bool:
    try:
        with psycopg.connect(DSN, connect_timeout=2):
            return True
    except psycopg.Error:
        return False


def test_suite_size() -> None:
    assert len(PAYLOADS) >= 150
    assert len(BENIGN) >= 60
    assert len({p["id"] for p in PAYLOADS}) == len(PAYLOADS)
    assert all(p["expect"] == "block" and p["category"] for p in PAYLOADS)


@pytest.mark.parametrize("payload", PAYLOADS, ids=[p["id"] for p in PAYLOADS])
def test_attack_is_blocked(payload: dict[str, Any]) -> None:
    result = validate_sql(payload["sql"])
    assert not result.is_valid, f"BYPASS [{payload['category']}]: {payload['sql']!r}"
    assert result.error


@pytest.mark.parametrize("payload", PAYLOADS, ids=[p["id"] for p in PAYLOADS])
def test_attack_is_blocked_with_table_allowlist(payload: dict[str, Any]) -> None:
    allowed = {"customers", "orders", "order_items", "products", "payments", "refunds"}
    assert not validate_sql(payload["sql"], allowed_tables=allowed).is_valid


@pytest.mark.parametrize("query", BENIGN, ids=[b["id"] for b in BENIGN])
def test_benign_query_is_valid(query: dict[str, Any]) -> None:
    result = validate_sql(query["sql"])
    assert result.is_valid, f"False block on {query['id']}: {result.error}"


@pytest.mark.integration
@pytest.mark.skipif(not _postgres_reachable(), reason="Postgres is not reachable")
@pytest.mark.parametrize("query", BENIGN, ids=[b["id"] for b in BENIGN])
def test_benign_query_executes(query: dict[str, Any]) -> None:
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("BEGIN READ ONLY")
        conn.execute("SET LOCAL statement_timeout='5s'")
        try:
            conn.execute(query["sql"]).fetchall()
        finally:
            conn.execute("ROLLBACK")


def test_every_benign_function_is_allowlisted() -> None:
    # guards against the allowlist silently drifting away from what analysts actually write
    assert {
        "count",
        "sum",
        "date_trunc",
        "string_agg",
        "row_number",
        "coalesce",
    } <= ALLOWED_FUNCTIONS


def test_allowlist_excludes_dangerous_functions() -> None:
    banned = {
        "query_to_xml",
        "current_setting",
        "set_config",
        "version",
        "pg_sleep",
        "pg_advisory_lock",
        "txid_current",
        "lo_from_bytea",
        "nextval",
        "dblink",
        "pg_ls_dir",
        "repeat",
    }
    assert not banned & ALLOWED_FUNCTIONS


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT query_to_xml('select * from pg_catalog.pg_roles', true, true, '')",
        "SELECT current_setting('data_directory')",
        "SELECT version()",
        "SELECT pg_ls_logdir()",
        "SELECT pg_advisory_lock(1)",
        "SELECT txid_current()",
        "SELECT lo_from_bytea(0,'x')",
        "SELECT * FROM generate_series(1, 1e11)",
    ],
)
def test_audit_section_1b_bypasses_are_closed(sql: str) -> None:
    result = validate_sql(sql)
    assert not result.is_valid
    assert result.is_safety_block


def test_unknown_function_is_a_terminal_block_with_informative_error() -> None:
    result = validate_sql("SELECT totally_made_up(name) FROM customers")
    assert result.is_safety_block
    assert "totally_made_up" in (result.error or "")
    assert "allowed-function list" in (result.error or "")


def test_schema_qualified_function_is_blocked() -> None:
    assert validate_sql("SELECT pg_catalog.lower('X')").is_safety_block
    assert validate_sql("SELECT public.lower('X')").is_safety_block


def test_parser_differential_backslash_is_blocked() -> None:
    # sqlglot hides the call inside a string; PostgreSQL executes pg_sleep
    result = validate_sql("SELECT 'a\\' || pg_sleep(5) || '...'")
    assert result.is_safety_block


def test_string_literal_containing_semicolon_is_still_valid() -> None:
    assert validate_sql("SELECT 'drop table; delete from orders' FROM agents").is_valid
    assert validate_sql("SELECT count(*) FROM orders;").is_valid
    assert validate_sql("SELECT 1 -- note; not a statement").is_valid


def test_table_allowlist() -> None:
    allowed = {"orders", "customers"}
    assert validate_sql("SELECT * FROM orders", allowed_tables=allowed).is_valid
    assert validate_sql("SELECT * FROM public.Orders o", allowed_tables=allowed).is_valid
    cte = "WITH x AS (SELECT * FROM orders) SELECT * FROM x"
    assert validate_sql(cte, allowed_tables=allowed).is_valid
    denied = validate_sql("SELECT * FROM payments", allowed_tables=allowed)
    assert not denied.is_valid
    assert not denied.is_safety_block  # unknown table: correctable, not terminal
    assert "payments" in (denied.error or "")
    sub = "SELECT * FROM orders WHERE order_id IN (SELECT order_id FROM payments)"
    assert not validate_sql(sub, allowed_tables=allowed).is_valid
    assert not validate_sql(sub, allowed_tables=allowed).is_safety_block
    # backward compatible: no allowlist means no table restriction
    assert validate_sql("SELECT * FROM payments").is_valid


def test_non_public_schema_is_blocked() -> None:
    assert validate_sql("SELECT * FROM restricted.canary").is_safety_block
    assert validate_sql("SELECT * FROM public.orders").is_valid


def test_unknown_table_is_correctable_but_system_schema_is_terminal() -> None:
    allowed = {"orders"}
    unknown = validate_sql("SELECT * FROM clients", allowed_tables=allowed)
    assert not unknown.is_valid and not unknown.is_blocked
    assert "clients" in (unknown.error or "")
    for sql in (
        "SELECT * FROM pg_catalog.pg_tables",
        "SELECT * FROM information_schema.tables",
        "SELECT * FROM pg_roles",
    ):
        assert validate_sql(sql, allowed_tables=allowed).is_blocked, sql
    # an unknown table listed first must not mask a system-schema reference
    mixed = "SELECT * FROM clients c JOIN pg_catalog.pg_tables t ON true"
    assert validate_sql(mixed, allowed_tables=allowed).is_blocked
    # other schemas stay terminal too
    assert validate_sql("SELECT * FROM secret.orders", allowed_tables=allowed).is_blocked


def test_table_allowlist_ctes_qualified_case_and_quoting() -> None:
    allowed = {"orders", "customers"}
    ok = [
        "WITH recent AS (SELECT * FROM orders) SELECT * FROM recent r JOIN customers c ON true",
        "WITH a AS (SELECT 1 AS x), b AS (SELECT x FROM a) SELECT * FROM b",
        "SELECT * FROM public.orders",
        'SELECT * FROM "public"."Orders"',
        'SELECT * FROM "ORDERS" o',
        "SELECT * FROM Customers",
        "SELECT * FROM (SELECT * FROM orders) s",
        "SELECT * FROM generate_series(1, 3) g",
    ]
    for sql in ok:
        res = validate_sql(sql, allowed_tables=allowed)
        assert res.is_valid, (sql, res.error)
    # a CTE name does not excuse a schema-qualified reference to a missing table
    res = validate_sql("WITH x AS (SELECT 1) SELECT * FROM public.x", allowed_tables=allowed)
    assert not res.is_valid and not res.is_blocked
    # no allowlist = no restriction (unchanged)
    assert validate_sql("SELECT * FROM clients").is_valid
