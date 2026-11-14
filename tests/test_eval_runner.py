"""Runner, checkpointing, LLM cache and variants, fully offline (fake LLM, in-memory SQL)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from nl2sql.agent.graph import build_graph
from nl2sql.agent.nodes import execute as execute_mod
from nl2sql.db.registry import DatabaseRegistry
from nl2sql.eval.dataset import Question, dataset_hash
from nl2sql.eval.llm_hook import CachedLLM, DiskCache, EvalLLMHook, UsageCollector, current_usage
from nl2sql.eval.metrics import Row
from nl2sql.eval.runner import RunConfig, load_checkpoint, run_questions
from nl2sql.eval.sqlrunner import SqlOutcome
from nl2sql.eval.variants import FullVariant, NaiveVariant, Prediction, build_ddl, clean_sql
from nl2sql.llm import providers
from nl2sql.llm.providers import FakeLLMScript
from nl2sql.models import ExecutionResult, PlanResult
from tests.fakes import StubSchemaProvider

GOLD_A = "SELECT COUNT(*) FROM t1"
GOLD_B = "SELECT a FROM t1 WHERE a > 1"


def _q(qid: int, text: str, gold: str, difficulty: str = "simple", db: str = "db1") -> Question:
    return Question(qid, db, text, f"hint {qid}", gold, difficulty)


QUESTIONS = [
    _q(1, "how many rows", GOLD_A),
    _q(2, "which values exceed one", GOLD_B, "moderate"),
    _q(3, "ask me something vague", GOLD_A, "challenging"),
]
TABLE_COLUMNS = {"db1": {"t1": ["a"]}}

DATA: dict[str, list[Row]] = {
    GOLD_A: [(5,)],
    GOLD_B: [(2,), (3,)],
    "SELECT COUNT(*) FROM t1 -- ok": [(5,)],
    "SELECT a FROM t1 WHERE a > 5": [(9,)],
    "SELECT nonsense": [],
}


class MemSql:
    """SqlRunner over a dict; unknown SQL is an error. Records every call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def run(self, db_id: str, sql: str, timeout_s: float = 30.0) -> SqlOutcome:
        self.calls.append((db_id, sql))
        if sql not in DATA or sql == "SELECT nonsense":
            return SqlOutcome(None, "UndefinedColumnError: nope", 0.001)
        return SqlOutcome(list(DATA[sql]), None, 0.001)


def _plan_for(msgs: list[dict[str, str]]) -> PlanResult:
    user = msgs[-1]["content"]
    system = msgs[0]["content"]
    if "vague" in user:
        return PlanResult(ambiguity_analysis="x", decision="clarify", clarification="which?")
    if "exceed" in user:
        return PlanResult(
            relevant_tables=["t1.a"],
            ambiguity_analysis="None",
            decision="sql",
            sql="SELECT a FROM t1 WHERE a > 5",
        )
    assert "hint 1" in system  # evidence reaches the planner prompt
    return PlanResult(
        relevant_tables=["t1.a", "t9.zzz"],
        ambiguity_analysis="None",
        decision="sql",
        sql="SELECT COUNT(*) FROM t1 -- ok",
    )


@pytest.fixture
def variant(
    fake_llm: FakeLLMScript, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> FullVariant:
    fake_llm.plan(_plan_for)
    seen: dict[str, Any] = {}

    async def fake_exec(pool: Any, sql: str, **kw: Any) -> ExecutionResult:
        seen.setdefault("row_caps", []).append(kw.get("row_cap", "default"))
        return ExecutionResult(success=True, columns=["x"], rows=[{"x": 1}], row_count=1)

    monkeypatch.setattr(execute_mod, "execute_sql", fake_exec)
    registry = DatabaseRegistry.from_single(None, StubSchemaProvider("TABLE t1 (a int)"), "db1")
    v = FullVariant(build_graph(registry=registry), EvalLLMHook(DiskCache(tmp_path / "cache")))
    v.seen = seen  # type: ignore[attr-defined]
    return v


async def test_full_variant_end_to_end_scoring(
    variant: FullVariant, fake_llm: FakeLLMScript
) -> None:
    cfg = RunConfig(concurrency=2, ves_repeats=2)
    recs = await run_questions(QUESTIONS, variant, MemSql(), TABLE_COLUMNS, cfg)
    by_id = {r["question_id"]: r for r in recs}
    assert [r["question_id"] for r in recs] == [1, 2, 3]  # input order

    assert by_id[1]["status"] == "answered" and by_id[1]["ex"] == 1
    assert by_id[1]["soft_f1"] == 1.0 and by_id[1]["ves_reward"] > 0
    assert by_id[2]["status"] == "answered" and by_id[2]["ex"] == 0  # wrong rows
    assert by_id[3]["status"] == "needs_clarification" and by_id[3]["ex"] == 0

    # eval mode lifts the row cap and skips synthesis (no 'fast' LLM call)
    assert set(variant.seen["row_caps"]) == {None}  # type: ignore[attr-defined]
    assert fake_llm.count("fast") == 0

    # schema linking against the gold SQL: t1 + t1.a gold; prediction adds a bogus table
    assert by_id[1]["link_table_recall"] == 1.0 and by_id[1]["link_table_precision"] == 0.5
    assert by_id[3]["link_table_recall"] is None  # no SQL plan -> not scored


async def test_evidence_switch(variant: FullVariant, fake_llm: FakeLLMScript) -> None:
    await run_questions(QUESTIONS[:1], variant, MemSql(), TABLE_COLUMNS, RunConfig(ves_repeats=0))
    assert "hint 1" in fake_llm.calls_for("plan")[0].text
    fake_llm.calls.clear()

    class NoEvidencePlan:
        pass

    # without evidence the prompt carries no hint block; plan fn asserts, so use another question
    await run_questions(
        QUESTIONS[1:2],
        variant,
        MemSql(),
        TABLE_COLUMNS,
        RunConfig(use_evidence=False, ves_repeats=0),
    )
    assert "hint 2" not in fake_llm.calls_for("plan")[0].text


async def test_checkpoint_resume_skips_finished_and_retries_infra(
    tmp_path: Path, variant: FullVariant
) -> None:
    cp = tmp_path / "run.jsonl"
    cfg = RunConfig(checkpoint=cp, ves_repeats=0)
    await run_questions(QUESTIONS[:2], variant, MemSql(), TABLE_COLUMNS, cfg)
    assert set(load_checkpoint(cp)) == {1, 2}

    calls: list[int] = []

    class Counting:
        name = "counting"

        async def predict(self, q: Question, use_evidence: bool) -> Prediction:
            calls.append(q.question_id)
            return Prediction(status="answered", sql=GOLD_A, decision="sql")

    recs = await run_questions(QUESTIONS, Counting(), MemSql(), TABLE_COLUMNS, cfg)
    assert calls == [3]  # 1 and 2 came from the checkpoint
    assert len(recs) == 3

    # an infra_error line is written but never counts as done
    with cp.open("a") as f:
        f.write('{"question_id": 3, "status": "infra_error"}\n{"torn')
    assert 3 not in load_checkpoint(cp) or load_checkpoint(cp)[3]["status"] != "infra_error"


async def test_infra_error_backoff_then_success_and_exhaustion() -> None:
    sleeps: list[float] = []
    attempts = {"n": 0}

    class Flaky:
        name = "flaky"

        async def predict(self, q: Question, use_evidence: bool) -> Prediction:
            attempts["n"] += 1
            if attempts["n"] < 3:
                return Prediction(
                    status="infra_error", llm_error="HTTP 429 rate limit", latency_s=9
                )
            return Prediction(status="answered", sql=GOLD_A, decision="sql", latency_s=0.5)

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    cfg = RunConfig(ves_repeats=0, infra_backoff_s=1.0)
    recs = await run_questions(
        QUESTIONS[:1], Flaky(), MemSql(), TABLE_COLUMNS, cfg, sleep=fake_sleep
    )
    assert recs[0]["status"] == "answered" and recs[0]["latency_s"] == 0.5  # backoff not in latency
    assert len(sleeps) == 2 and sleeps[1] > sleeps[0] * 0.5
    assert all(s >= 2.0 for s in sleeps[:1])  # 429 backs off 4x harder than other errors

    class Down:
        name = "down"

        async def predict(self, q: Question, use_evidence: bool) -> Prediction:
            return Prediction(status="infra_error", llm_error="503 unavailable")

    sleeps.clear()
    recs = await run_questions(
        QUESTIONS[:1],
        Down(),
        MemSql(),
        TABLE_COLUMNS,
        RunConfig(ves_repeats=0, max_infra_retries=2),
        sleep=fake_sleep,
    )
    assert recs[0]["status"] == "infra_error" and len(sleeps) == 2


async def test_request_too_large_is_not_retried() -> None:
    sleeps: list[float] = []
    n = {"calls": 0}

    class TooBig:
        name = "toobig"

        async def predict(self, q: Question, use_evidence: bool) -> Prediction:
            n["calls"] += 1
            return Prediction(status="infra_error", llm_error="Error code: 413 - Request too large")

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    recs = await run_questions(
        QUESTIONS[:1], TooBig(), MemSql(), TABLE_COLUMNS, RunConfig(ves_repeats=0), sleep=fake_sleep
    )
    assert recs[0]["status"] == "infra_error" and n["calls"] == 1 and sleeps == []


async def test_bounded_concurrency() -> None:
    import asyncio

    active = {"now": 0, "max": 0}

    class Slow:
        name = "slow"

        async def predict(self, q: Question, use_evidence: bool) -> Prediction:
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
            await asyncio.sleep(0.01)
            active["now"] -= 1
            return Prediction(status="refused", decision="refuse")

    qs = [_q(i, "q", GOLD_A) for i in range(10, 20)]
    await run_questions(
        qs, Slow(), MemSql(), TABLE_COLUMNS, RunConfig(concurrency=3, ves_repeats=0)
    )
    assert active["max"] == 3


async def test_gold_error_and_pred_error_are_recorded_not_raised() -> None:
    class Bad:
        name = "bad"

        async def predict(self, q: Question, use_evidence: bool) -> Prediction:
            return Prediction(status="answered", sql="SELECT nonsense", decision="sql")

    recs = await run_questions(
        [_q(1, "q", GOLD_A)], Bad(), MemSql(), TABLE_COLUMNS, RunConfig(ves_repeats=0)
    )
    assert recs[0]["ex"] == 0 and "UndefinedColumn" in recs[0]["pred_error"]


# ── LLM cache + usage ────────────────────────────────────────────────────────


async def test_cache_hit_replays_response_and_usage(
    fake_llm: FakeLLMScript, tmp_path: Path
) -> None:
    fake_llm.usage = (100, 20)
    fake_llm.correct("SELECT 1")
    cache = DiskCache(tmp_path)
    llm = CachedLLM(providers.get_planner_llm(), cache, "planner", "fake-planner")
    collector = UsageCollector()
    tok = current_usage.set(collector)
    try:
        cfg = {"metadata": {"langgraph_node": "correct_sql"}}
        a = await llm.ainvoke([{"role": "user", "content": "q"}], cfg)  # type: ignore[arg-type]
        b = await llm.ainvoke([{"role": "user", "content": "q"}], cfg)  # type: ignore[arg-type]
        await llm.ainvoke([{"role": "user", "content": "different"}], cfg)  # type: ignore[arg-type]
    finally:
        current_usage.reset(tok)
    assert a.content == b.content == "SELECT 1"
    assert fake_llm.count("correct") == 2  # second identical call never reached the model
    assert (cache.hits, cache.misses) == (1, 2)
    u = collector.to_dict()["correct_sql"]["fake-planner"]
    assert u == {"calls": 3, "cached_calls": 1, "input_tokens": 300, "output_tokens": 60}


async def test_no_cache_always_calls_model(fake_llm: FakeLLMScript) -> None:
    llm = CachedLLM(providers.get_planner_llm(), None, "planner", "fake-planner")
    for _ in range(2):
        await llm.ainvoke([{"role": "user", "content": "q"}])
    assert fake_llm.count("correct") == 2


async def test_cache_key_depends_on_model_and_structured_schema(
    fake_llm: FakeLLMScript, tmp_path: Path
) -> None:
    cache = DiskCache(tmp_path)
    fake_llm.plan(PlanResult(ambiguity_analysis="x", decision="refuse", reason="r"))
    msgs = [{"role": "user", "content": "q"}]
    m1 = CachedLLM(providers.get_planner_llm(), cache, "planner", "model-a")
    m2 = CachedLLM(providers.get_planner_llm(), cache, "planner", "model-b")
    r1 = await m1.with_structured_output(PlanResult, method="json_mode").ainvoke(msgs)
    r1b = await m1.with_structured_output(PlanResult, method="json_mode").ainvoke(msgs)
    await m2.with_structured_output(PlanResult, method="json_mode").ainvoke(msgs)
    assert isinstance(r1, PlanResult) and r1b == r1
    assert fake_llm.count("plan") == 2  # model-a once (second was a hit), model-b once


def test_disk_cache_roundtrip_and_corruption(tmp_path: Path) -> None:
    c = DiskCache(tmp_path)
    key = DiskCache.make_key({"a": 1})
    assert c.get(key) is None
    c.put(key, {"x": 1})
    assert c.get(key) == {"x": 1}
    c._path(key).write_text("{torn")
    assert c.get(key) is None


# ── Naive variant ─────────────────────────────────────────────────────────────


async def test_naive_variant_single_prompt_no_correction(
    fake_llm: FakeLLMScript, tmp_path: Path
) -> None:
    fake_llm.correct("```sql\nSELECT COUNT(*) FROM t1;\n```")
    profile = {
        "tables": [
            {
                "name": "t1",
                "columns": [{"name": "a", "type": "integer", "nullable": False}],
                "primary_key": ["a"],
                "foreign_keys": [{"column": "a", "foreign_table": "t0", "foreign_column": "id"}],
            }
        ]
    }
    ddl = build_ddl(profile)
    assert 'CREATE TABLE "t1"' in ddl and "PRIMARY KEY" in ddl and "REFERENCES" in ddl
    v = NaiveVariant({"db1": ddl}, EvalLLMHook(None))
    pred = await v.predict(QUESTIONS[0], use_evidence=True)
    assert pred.status == "answered" and pred.sql == "SELECT COUNT(*) FROM t1"
    calls = fake_llm.calls_for("correct")
    assert len(calls) == 1 and fake_llm.count("plan") == 0
    assert "CREATE TABLE" in calls[0].text and "hint 1" in calls[0].text
    assert "Sample rows" not in calls[0].text  # no profiling

    pred2 = await v.predict(QUESTIONS[0], use_evidence=False)
    assert "hint 1" not in fake_llm.calls_for("correct")[1].text
    assert pred2.status == "answered"


async def test_naive_llm_failure_is_infra_error(fake_llm: FakeLLMScript) -> None:
    fake_llm.correct(RuntimeError("boom"))
    v = NaiveVariant({"db1": "ddl"}, EvalLLMHook(None))
    pred = await v.predict(QUESTIONS[0], use_evidence=False)
    assert pred.status == "infra_error" and "boom" in pred.llm_error


def test_clean_sql_and_dataset_hash() -> None:
    assert clean_sql("```SQL\nSELECT 1;\n```") == "SELECT 1"
    assert clean_sql("  SELECT 2  ") == "SELECT 2"
    assert dataset_hash(QUESTIONS) == dataset_hash(list(reversed(QUESTIONS)))
    assert dataset_hash(QUESTIONS) != dataset_hash(QUESTIONS[:2])


async def test_llm_error_in_full_graph_is_infra_error(
    fake_llm: FakeLLMScript, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_llm.plan(RuntimeError("HTTP 429 too many requests"))
    registry = DatabaseRegistry.from_single(None, StubSchemaProvider(), "db1")
    v = FullVariant(build_graph(registry=registry), EvalLLMHook(None))
    pred = await v.predict(QUESTIONS[0], use_evidence=False)
    assert pred.status == "infra_error" and "429" in pred.llm_error
