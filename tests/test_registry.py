"""DatabaseRegistry, per-request db routing in the graph, and profile rendering (offline)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import asyncpg
import pytest

from nl2sql.agent.graph import build_graph, run_query
from nl2sql.agent.nodes import execute as execute_mod
from nl2sql.db.registry import DatabaseRegistry, UnknownDatabaseError
from nl2sql.llm.providers import FakeLLMScript
from nl2sql.models import ExecutionResult
from nl2sql.schema.introspect import (
    PROFILE_FORMAT_VERSION,
    _absolute_n_distinct,
    estimate_tokens,
    render_context,
    render_table,
    sanitize_value,
)
from nl2sql.schema.store import StoredProfileSchemaProvider, load_profile, save_profile
from tests.fakes import StubSchemaProvider, sql_plan


async def test_unknown_database_raises_with_known_ids() -> None:
    reg = DatabaseRegistry.from_single(None, StubSchemaProvider(), "alpha")
    with pytest.raises(UnknownDatabaseError, match="alpha"):
        await reg.get("beta")
    assert (await reg.get(None)).db_id == "alpha"  # default
    assert (await reg.get("alpha")).schema_provider is not None


async def test_pools_are_created_lazily_once_and_closed() -> None:
    created: list[int] = []

    class Pool:
        closed = False

        async def close(self) -> None:
            self.closed = True

    pools: list[Pool] = []

    async def factory() -> Any:
        created.append(1)
        pools.append(Pool())
        return pools[-1]

    reg = DatabaseRegistry("a")
    reg.register("a", pool_factory=factory, schema_provider=StubSchemaProvider())
    reg.register("b", pool_factory=factory, schema_provider=StubSchemaProvider())
    assert created == []
    import asyncio

    r1, r2 = await asyncio.gather(reg.get("a"), reg.get("a"))
    assert r1 is r2 and created == [1]
    await reg.close()
    assert pools[0].closed


async def test_caller_supplied_pool_is_not_closed() -> None:
    class Pool:
        closed = False

        async def close(self) -> None:
            self.closed = True

    pool = Pool()
    reg = DatabaseRegistry.from_single(pool, StubSchemaProvider())
    await reg.get(None)
    await reg.close()
    assert not pool.closed


async def test_graph_routes_schema_and_pool_per_request_db_id(
    fake_llm: FakeLLMScript, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool_a, pool_b = object(), object()
    reg = DatabaseRegistry("a")
    reg.register("a", pool=pool_a, schema_provider=StubSchemaProvider("SCHEMA-OF-A"))
    reg.register("b", pool=pool_b, schema_provider=StubSchemaProvider("SCHEMA-OF-B"))
    used: list[Any] = []

    async def fake_exec(pool: Any, sql: str) -> ExecutionResult:
        used.append(pool)
        return ExecutionResult(success=True, columns=["x"], rows=[{"x": 1}], row_count=1)

    monkeypatch.setattr(execute_mod, "execute_sql", fake_exec)
    fake_llm.plan(sql_plan("SELECT 1 AS x"))
    graph = build_graph(registry=reg)

    sb = await run_query(graph, "q", db_id="b")
    sa = await run_query(graph, "q")  # default database
    assert sb["db_id"] == "b" and sa["db_id"] == "a"
    assert "SCHEMA-OF-B" in fake_llm.calls_for("plan")[0].text
    assert "SCHEMA-OF-A" in fake_llm.calls_for("plan")[1].text
    assert used == [pool_b, pool_a]


async def test_unknown_db_id_fails_before_any_llm_call(fake_llm: FakeLLMScript) -> None:
    graph = build_graph(registry=DatabaseRegistry.from_single(None, StubSchemaProvider(), "a"))
    state = await run_query(graph, "q", db_id="nope")
    assert state["status"] == "failed" and "nope" in state["final_answer"]
    assert fake_llm.count("plan") == 0


async def test_legacy_single_pool_build_graph_still_works(
    fake_llm: FakeLLMScript, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_exec(pool: Any, sql: str) -> ExecutionResult:
        return ExecutionResult(success=True, columns=["x"], rows=[{"x": 7}], row_count=1)

    monkeypatch.setattr(execute_mod, "execute_sql", fake_exec)
    fake_llm.plan(sql_plan("SELECT 7 AS x"))
    state = await run_query(build_graph(None, schema_provider=StubSchemaProvider()), "q")
    assert state["status"] == "answered" and "7" in state["final_answer"]


# ── profiling: pure functions ────────────────────────────────────────────────


def _profile() -> dict[str, Any]:
    long_text = "x" * 500 + "\nnewline"
    return {
        "format_version": PROFILE_FORMAT_VERSION,
        "db_id": "demo",
        "display_name": "demo",
        "domain_description": None,
        "date_ranges": {"t.d": ["2000-01-01", "2001-01-01"]},
        "tables": [
            {
                "name": "t",
                "rows": 1000,
                "rows_estimated": True,
                "columns": [
                    {"name": "k", "type": "bigint", "nullable": False},
                    {
                        "name": "status",
                        "type": "text",
                        "nullable": True,
                        "n_distinct": 2,
                        "values": ["open", "closed"],
                        "values_complete": True,
                    },
                    {
                        "name": "city",
                        "type": "text",
                        "nullable": True,
                        "n_distinct": 5000,
                        "values": [f"city{i}" for i in range(30)],
                        "values_complete": False,
                    },
                    {
                        "name": "d",
                        "type": "date",
                        "nullable": True,
                        "range": ["2000-01-01", "2001-01-01"],
                    },
                ],
                "primary_key": ["k"],
                "foreign_keys": [],
                "checks": [],
                "samples": [{"k": 1, "status": "open", "city": sanitize_value(long_text)}] * 3,
            }
        ],
    }


def test_render_labels_complete_vs_common_values_and_estimates() -> None:
    text = render_table(_profile()["tables"][0])
    assert "(~1000 rows)" in text
    assert "Distinct values: ['open', 'closed']" in text
    assert "Common values (of ~5000)" in text
    assert text.count("'city") == 8  # non-exhaustive lists are capped
    assert "Range: 2000-01-01 to 2001-01-01" in text
    assert "Primary Key: k" in text


def test_sample_values_are_truncated_and_single_line() -> None:
    out = sanitize_value("a" * 500 + "\nb")
    assert len(out) == 60 and out.endswith("...") and "\n" not in out
    assert sanitize_value("tab\tand\x00ctrl") == "tab and ctrl"


def test_token_budget_degrades_each_table() -> None:
    p = _profile()
    full = estimate_tokens(render_context(p, 10_000))
    tight = estimate_tokens(render_context(p, 60))
    assert tight < full
    assert "Sample rows" not in render_context(p, 60)
    assert "Sample rows" in render_context(p, 10_000)
    assert "k (bigint" in render_context(p, 1)  # column list is never dropped


def test_n_distinct_conversion() -> None:
    assert _absolute_n_distinct(12, 1000) == 12
    assert _absolute_n_distinct(-0.5, 1000) == 500  # negative = fraction of rows
    assert _absolute_n_distinct(None, 1000) is None


async def test_stored_profile_roundtrip_and_provider(tmp_path: Path) -> None:
    path = save_profile(_profile(), tmp_path)
    data = load_profile(path)
    assert data["content_sha256"] and data["built_at"]
    provider = StoredProfileSchemaProvider(data)
    ctx = await provider.get_context("anything")
    assert ctx.startswith("=== DATABASE SCHEMA (demo) ===")
    assert provider.profile.date_ranges["t.d"] == ("2000-01-01", "2001-01-01")
    # content hash ignores the build timestamp
    assert save_profile(_profile(), tmp_path).exists()
    assert load_profile(path)["content_sha256"] == data["content_sha256"]


def test_incompatible_profile_version_rejected(tmp_path: Path) -> None:
    bad = _profile()
    bad["format_version"] = 999
    p = tmp_path / "x.json"
    import json

    p.write_text(json.dumps(bad))
    with pytest.raises(ValueError, match="rebuild"):
        load_profile(p)


async def test_registry_prefers_stored_profile(tmp_path: Path) -> None:
    save_profile(_profile(), tmp_path)
    reg = DatabaseRegistry("demo")
    reg.register_dsn("demo", "postgresql://u:p@localhost:1/none", profile_dir=tmp_path)
    res = await reg.get("demo")  # pool is lazy: no connection attempted for the profile
    assert isinstance(res.schema_provider, StoredProfileSchemaProvider)
    assert res.profile is not None and res.profile.db_id == "demo"
    assert isinstance(res.pool, asyncpg.Pool) or res.pool is not None
    await reg.close()
