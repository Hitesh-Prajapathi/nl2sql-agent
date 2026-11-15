"""Offline tests for the clarification eval (evalsets/clarification): dataset + runner + metrics.

Fake LLM only; results are written to tmp_path (never results/).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from nl2sql.agent.graph import build_graph, run_query
from nl2sql.agent.nodes import execute as execute_mod
from nl2sql.llm.providers import FakeLLMScript
from nl2sql.safety.validator import detect_prompt_injection
from tests.fakes import (
    FakeExecutor,
    SetSettings,
    StubSchemaProvider,
    clarify_plan,
    ok_result,
    sql_plan,
)

DIR = Path(__file__).resolve().parent.parent / "evalsets" / "clarification"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("clar_eval", DIR / "run_clarification_eval.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["clar_eval"] = mod
    spec.loader.exec_module(mod)
    return mod


ev = _load()
ITEMS = ev.load_items()
AMBIGUITY_TYPES = {
    "undefined_metric",
    "ranking_criterion",
    "ambiguous_entity",
    "time_range",
    "threshold",
    "column_ambiguity",
}


# ── dataset ──────────────────────────────────────────────────────────────────


def test_dataset_shape() -> None:
    amb = [i for i in ITEMS if i["label"] == "ambiguous"]
    clr = [i for i in ITEMS if i["label"] == "clear"]
    assert (len(amb), len(clr)) == (50, 50)
    assert len({i["id"] for i in ITEMS}) == 100
    assert len({i["question"] for i in ITEMS}) == 100
    for i in amb:
        assert i["ambiguity_type"] in AMBIGUITY_TYPES and len(i["note"]) > 10, i["id"]
    assert {i["ambiguity_type"] for i in amb} == AMBIGUITY_TYPES
    assert {i["difficulty"] for i in clr} == {"easy", "near_miss"}


def test_questions_do_not_trip_the_regex_guard() -> None:
    """We measure the planner's judgement, not the deterministic injection guard."""
    assert [i["id"] for i in ITEMS if detect_prompt_injection(i["question"])] == []


def test_clear_questions_do_not_use_ambiguous_names() -> None:
    for i in ITEMS:
        if i["label"] == "clear":
            assert "Rahul Menon" not in i["question"] or "@" in i["question"], i["id"]
            assert "Priya Verma" not in i["question"], i["id"]


@pytest.mark.integration
async def test_clear_item_entities_exist_and_are_unique() -> None:
    """Names / keys used by clear questions resolve to exactly one row in the demo database."""
    import asyncpg

    from nl2sql.config import get_settings

    conn = await asyncpg.connect(get_settings().database_url)
    try:
        assert await conn.fetchval("SELECT count(*) FROM agents WHERE name = 'Rohan Iyer'") == 1
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM customers WHERE email = 'rahul.menon9@example.com'"
            )
            == 1
        )
        assert await conn.fetchval("SELECT count(*) FROM products WHERE sku = 'SKU-0003'") == 1
        # the ambiguity traps the ambiguous set relies on really exist
        assert await conn.fetchval("SELECT count(*) FROM customers WHERE name = 'Rahul Menon'") == 2
        assert (
            await conn.fetchval("SELECT count(*) FROM customers c JOIN agents a ON a.name = c.name")
            >= 1
        )
    finally:
        await conn.close()


# ── metrics ──────────────────────────────────────────────────────────────────


def test_prf_known_values() -> None:
    m = ev.prf(tp=40, fn=10, fp=5, tn=45)
    assert m["precision"] == pytest.approx(40 / 45)
    assert m["recall"] == pytest.approx(0.8)
    assert m["f1"] == pytest.approx(2 * (40 / 45) * 0.8 / ((40 / 45) + 0.8))
    assert m["false_clarify_rate"] == pytest.approx(0.1)


def test_prf_degenerate_cases() -> None:
    never = ev.prf(tp=0, fn=50, fp=0, tn=50)  # never clarifies
    assert never["precision"] is None and never["recall"] == 0.0 and never["f1"] == 0.0
    always = ev.prf(tp=50, fn=0, fp=50, tn=0)
    assert always["precision"] == 0.5 and always["recall"] == 1.0
    assert always["false_clarify_rate"] == 1.0
    perfect = ev.prf(tp=50, fn=0, fp=0, tn=50)
    assert perfect["f1"] == 1.0 and perfect["false_clarify_rate"] == 0.0


def test_stratified_bootstrap_brackets_the_point_estimate() -> None:
    amb = [1] * 40 + [0] * 10
    clr = [1] * 5 + [0] * 45
    ci = ev.stratified_bootstrap(amb, clr, n_resamples=500, seed=1)
    point = ev.prf(tp=40, fn=10, fp=5, tn=45)
    for k, (lo, hi) in ((k, v) for k, v in ci.items() if v):
        assert 0.0 <= lo <= hi <= 1.0
        assert lo <= point[k] <= hi, k  # type: ignore[operator]
    assert ev.stratified_bootstrap(amb, clr, n_resamples=500, seed=1) == ci  # deterministic


def test_bootstrap_zero_positive_predictions() -> None:
    ci = ev.stratified_bootstrap([0] * 50, [0] * 50, n_resamples=100)
    assert ci["precision"] is None  # undefined in every resample
    assert ci["recall"] == [0.0, 0.0] and ci["f1"] == [0.0, 0.0]
    assert ev.stratified_bootstrap([], [1, 0])["recall"] is None


def _rec(i: str, label: str, asked: bool | None, err: str = "") -> dict[str, Any]:
    r: dict[str, Any] = {"id": i, "label": label, "error": err, "ambiguity_type": "threshold"}
    if not err:
        r.update(asked=asked, status="needs_clarification" if asked else "answered", usage={})
    return r


def test_summarize_excludes_errors() -> None:
    recs = [
        _rec("a1", "ambiguous", True),
        _rec("a2", "ambiguous", False),
        _rec("a3", "ambiguous", None, "llm_error: x"),
        _rec("c1", "clear", True),
        _rec("c2", "clear", False),
        _rec("c3", "clear", False),
    ]
    s = ev.summarize(recs)
    assert s["confusion"] == {"tp": 1, "fn": 1, "fp": 1, "tn": 2}
    assert s["n_errors"] == 1 and s["n_ambiguous"] == 2 and s["n_clear"] == 3
    assert s["recall"]["value"] == 0.5 and s["precision"]["value"] == 0.5
    assert s["false_clarify_rate"]["value"] == pytest.approx(1 / 3)
    assert s["false_clarify_ids"] == ["c1"] and s["missed_ambiguous_ids"] == ["a2"]
    assert s["recall_by_ambiguity_type"]["threshold"] == {"asked": 1, "n": 2, "recall": 0.5}
    lo, hi = s["recall"]["ci95"]
    assert lo <= 0.5 <= hi


# ── runner with the fake LLM ─────────────────────────────────────────────────


@pytest.fixture
def ask(fake_llm: FakeLLMScript, monkeypatch: pytest.MonkeyPatch, set_settings: SetSettings) -> Any:
    set_settings(max_retry_attempts=1)
    monkeypatch.setattr(execute_mod, "execute_sql", FakeExecutor(ok_result()))
    graph = build_graph(None, schema_provider=StubSchemaProvider())

    async def _ask(question: str, thread_id: str) -> dict[str, Any]:
        res = dict(await run_query(graph, question, thread_id=thread_id))
        res["_usage"] = {"llm_calls": 1, "input_tokens": 10, "output_tokens": 5}
        return res

    return _ask


def _oracle_planner(ambiguous_ids: set[str]) -> Any:
    """Fake planner that clarifies exactly the questions of the given items."""
    by_q = {i["question"]: i["id"] for i in ITEMS}

    def plan(msgs: list[dict[str, str]]) -> Any:
        return (
            clarify_plan("Could you be more specific?")
            if by_q[msgs[-1]["content"]] in ambiguous_ids
            else sql_plan("SELECT COUNT(*) AS total FROM customers")
        )

    return plan


async def test_perfect_planner_scores_perfectly(ask: Any, fake_llm: FakeLLMScript) -> None:
    fake_llm.plan(_oracle_planner({i["id"] for i in ITEMS if i["label"] == "ambiguous"}))
    recs = await ev.run_suite(ITEMS, ask)
    s = ev.summarize(recs)
    assert s["confusion"] == {"tp": 50, "fn": 0, "fp": 0, "tn": 50}
    assert s["f1"]["value"] == 1.0 and s["false_clarify_rate"]["value"] == 0.0
    assert s["usage"]["llm_calls"] == 100


async def test_always_clarify_planner(ask: Any, fake_llm: FakeLLMScript) -> None:
    fake_llm.plan(lambda msgs: clarify_plan("what do you mean?"))
    s = ev.summarize(await ev.run_suite(ITEMS, ask))
    assert s["recall"]["value"] == 1.0 and s["precision"]["value"] == 0.5
    assert s["false_clarify_rate"]["value"] == 1.0
    assert s["status_on_clear"] == {"needs_clarification": 50}


async def test_never_clarify_planner(ask: Any, fake_llm: FakeLLMScript) -> None:
    fake_llm.plan(lambda msgs: sql_plan("SELECT 1"))
    s = ev.summarize(await ev.run_suite(ITEMS, ask))
    assert s["recall"]["value"] == 0.0 and s["precision"]["value"] is None
    assert s["f1"]["value"] == 0.0 and s["false_clarify_rate"]["value"] == 0.0


async def test_llm_outage_is_excluded_not_counted(ask: Any, fake_llm: FakeLLMScript) -> None:
    down = {ITEMS[0]["question"], ITEMS[1]["question"]}
    fake_llm.plan(
        lambda msgs: (
            RuntimeError("provider down") if msgs[-1]["content"] in down else sql_plan("SELECT 1")
        )
    )
    recs = await ev.run_suite(ITEMS[:3], ask)
    assert [bool(r["error"]) for r in recs] == [True, True, False]
    s = ev.summarize(recs)
    assert s["n_errors"] == 2 and s["n_ambiguous"] + s["n_clear"] == 1


async def test_checkpoint_resume_skips_done_and_retries_errors(
    ask: Any, fake_llm: FakeLLMScript, tmp_path: Path
) -> None:
    fake_llm.plan(lambda msgs: sql_plan("SELECT 1"))
    ckpt = tmp_path / "c.jsonl"
    items = ITEMS[:4]
    await ev.run_suite(items, ask, checkpoint=ckpt)
    assert len(ckpt.read_text().splitlines()) == 4
    done = ev.load_checkpoint(ckpt)
    before = fake_llm.count("plan")
    await ev.run_suite(items, ask, checkpoint=ckpt, done=done)
    assert fake_llm.count("plan") == before
    ckpt.write_text(json.dumps({"id": items[0]["id"], "error": "llm_error: x"}) + "\n")
    assert ev.load_checkpoint(ckpt) == {}


def test_select_items_limit_per_label() -> None:
    sel = ev.select_items(ITEMS, None, 3)
    assert sum(i["label"] == "ambiguous" for i in sel) == 3
    assert sum(i["label"] == "clear" for i in sel) == 3
    assert [i["id"] for i in ev.select_items(ITEMS, "amb-001,clr-002", None)] == [
        "amb-001",
        "clr-002",
    ]


async def test_refuses_real_provider_without_confirmation(
    set_settings: SetSettings, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    set_settings(llm_provider="groq")
    ns = argparse.Namespace(
        label="t",
        out_dir=str(tmp_path),
        ids=None,
        limit=1,
        concurrency=1,
        resume=True,
        confirm_real_llm=False,
        budget=False,
    )
    assert await ev.amain(ns) == 2
    assert "refusing to run" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []  # noqa: ASYNC240


async def test_results_files_roundtrip(ask: Any, fake_llm: FakeLLMScript, tmp_path: Path) -> None:
    fake_llm.plan(_oracle_planner({i["id"] for i in ITEMS if i["label"] == "ambiguous"}))
    recs = await ev.run_suite(ITEMS, ask)
    import datetime as dt

    res = ev.build_results(
        label="unit",
        records=recs,
        n_expected=100,
        flags={},
        started=dt.datetime.now(dt.UTC),
        wall_s=1.0,
    )
    assert res["complete"] is True and res["meta"]["dataset_sha256"] == ev.dataset_sha256()
    j, m = ev.write_results(res, tmp_path, "unit")
    assert j.name == "clarification_unit.json" and m.name == "clarification_unit.md"
    md = m.read_text()
    assert "precision" in md and "false-clarify" in md and "Recall by ambiguity type" in md
    assert json.loads(j.read_text())["summary"]["confusion"]["tp"] == 50


def test_budget_estimate() -> None:
    b = ev.budget_estimate(ITEMS)
    p = b["planner"]
    assert p["floor"]["requests"] == 100 and p["worst"]["requests"] == 400
    assert p["floor"]["tokens"] == 100 * b["tokens_per_request"]
    assert p["floor"]["days"] * 200_000 >= p["floor"]["tokens"]
    assert b["daily_caps"]["planner_requests_per_day_at_cap"] == 200_000 // b["tokens_per_request"]
