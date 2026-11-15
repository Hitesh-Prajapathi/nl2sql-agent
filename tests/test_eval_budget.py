"""Daily-budget stop, resumable checkpoint identity, sampling and the small naive variant."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from nl2sql.eval.cli import _sample_per_difficulty
from nl2sql.eval.dataset import Question
from nl2sql.eval.llm_hook import EvalLLMHook
from nl2sql.eval.runner import (
    CheckpointMismatch,
    RunConfig,
    RunState,
    load_checkpoint,
    run_questions,
)
from nl2sql.eval.variants import NaiveVariant, Prediction
from nl2sql.llm.pacer import DAILY_MARKER, DailyBudgetExhausted, Pacer
from tests.test_eval_runner import GOLD_A, TABLE_COLUMNS, MemSql

QS = [
    Question(i, "db1", f"question {i}", "", GOLD_A, ["simple", "moderate", "challenging"][i % 3])
    for i in range(1, 7)
]


class Answering:
    name = "answering"

    def __init__(self) -> None:
        self.calls: list[int] = []

    async def predict(self, q: Question, use_evidence: bool) -> Prediction:
        self.calls.append(q.question_id)
        return Prediction(status="answered", sql=GOLD_A, decision="sql")


async def test_run_stops_cleanly_when_the_daily_budget_cannot_cover_another_question(
    tmp_path: Path,
) -> None:
    cp = tmp_path / "run.jsonl"
    pacer2 = Pacer(rpd=1000, rpd_margin=0)
    for _ in range(994):  # 6 left: two questions use 2 each, then 2 < 4 needed -> stop
        await pacer2.acquire("planner", 1)
    state = RunState()
    cfg2 = RunConfig(
        checkpoint=cp,
        ves_repeats=0,
        concurrency=1,
        pacer=pacer2,
        budget_models=("planner",),
        calls_per_question=4,
        state=state,
    )

    class Charging2(Answering):
        async def predict(self, q: Question, use_evidence: bool) -> Prediction:
            await pacer2.acquire("planner", 1)
            await pacer2.acquire("planner", 1)
            return await super().predict(q, use_evidence)

    v2 = Charging2()
    recs = await run_questions(QS, v2, MemSql(), TABLE_COLUMNS, cfg2)
    assert [r["question_id"] for r in recs] == [1, 2]
    assert state.stopped_reason and "daily request budget" in state.stopped_reason
    assert state.skipped == len(QS) - 2
    assert set(load_checkpoint(cp)) == {1, 2}  # resumable: finished work is on disk


async def test_budget_exhausted_mid_question_is_not_recorded_as_done(tmp_path: Path) -> None:
    cp = tmp_path / "run.jsonl"
    state = RunState()

    class Exhausted:
        name = "x"

        async def predict(self, q: Question, use_evidence: bool) -> Prediction:
            return Prediction(
                status="infra_error",
                llm_error=f"LLM error: {DAILY_MARKER} exhausted for m (995/1000 requests today)",
            )

    sleeps: list[float] = []

    async def no_sleep(s: float) -> None:
        sleeps.append(s)

    recs = await run_questions(
        QS[:3],
        Exhausted(),
        MemSql(),
        TABLE_COLUMNS,
        RunConfig(checkpoint=cp, ves_repeats=0, concurrency=1, state=state),
        sleep=no_sleep,
    )
    assert recs == [] and sleeps == []  # no retries, nothing recorded
    assert state.stopped_reason and state.skipped == 3
    assert load_checkpoint(cp) == {}


async def test_resume_continues_from_the_checkpoint_with_the_same_identity(
    tmp_path: Path,
) -> None:
    cp = tmp_path / "bird_minidev_full_small.jsonl"  # no date in the name
    fp = {
        "variant": "full_small",
        "planner_model": "openai/gpt-oss-120b",
        "prompt_cap_tokens": 6500,
    }
    v1 = Answering()
    await run_questions(
        QS[:3], v1, MemSql(), TABLE_COLUMNS, RunConfig(checkpoint=cp, ves_repeats=0, fingerprint=fp)
    )
    v2 = Answering()
    recs = await run_questions(
        QS, v2, MemSql(), TABLE_COLUMNS, RunConfig(checkpoint=cp, ves_repeats=0, fingerprint=fp)
    )
    assert sorted(v2.calls) == [4, 5, 6] and len(recs) == 6


async def test_checkpoint_from_a_different_configuration_is_refused(tmp_path: Path) -> None:
    cp = tmp_path / "run.jsonl"
    old = {"variant": "full_small", "planner_model": "llama-3.3-70b", "prompt_cap_tokens": 6500}
    new = {**old, "planner_model": "openai/gpt-oss-120b"}
    await run_questions(
        QS[:1],
        Answering(),
        MemSql(),
        TABLE_COLUMNS,
        RunConfig(checkpoint=cp, ves_repeats=0, fingerprint=old),
    )
    with pytest.raises(CheckpointMismatch) as ei:
        await run_questions(
            QS,
            Answering(),
            MemSql(),
            TABLE_COLUMNS,
            RunConfig(checkpoint=cp, ves_repeats=0, fingerprint=new),
        )
    assert "planner_model" in str(ei.value)


def test_sample_per_difficulty_is_seeded_and_balanced() -> None:
    qs = [
        Question(i, "db", "q", "", "s", ["simple", "moderate", "challenging"][i % 3])
        for i in range(1, 91)
    ]
    a = _sample_per_difficulty(qs, 5, seed=7)
    b = _sample_per_difficulty(qs, 5, seed=7)
    c = _sample_per_difficulty(qs, 5, seed=8)
    assert a == b and a != c and len(a) == 15
    for level in ("simple", "moderate", "challenging"):
        assert sum(1 for q in a if q.difficulty == level) == 5


async def test_naive_small_cuts_the_ddl_to_the_budget(fake_llm: Any, tmp_path: Path) -> None:
    profile = {
        "db_id": "db1",
        "tables": [
            {
                "name": f"tbl{i}",
                "rows": 1,
                "columns": [
                    {"name": f"col{j}", "type": "text", "nullable": True} for j in range(40)
                ],
                "primary_key": [],
                "foreign_keys": [],
            }
            for i in range(20)
        ],
    }
    captured: list[str] = []
    fake_llm.correct(lambda msgs: (captured.append(msgs[0]["content"]), "SELECT 1")[1])
    v = NaiveVariant(
        {},
        EvalLLMHook(None),
        name="naive_small",
        profiles={"db1": profile},
        total_cap_tokens=3000,
        max_output_tokens=2048,
    )
    q = Question(1, "db1", "values of col3 in tbl7", "", "SELECT 1", "simple")
    pred = await v.predict(q, True)
    assert pred.status == "answered" and pred.llm_calls[0]["model"] == "fake-planner"
    system = captured[0]
    assert 'CREATE TABLE "tbl7"' in system and '"col3"' in system
    assert 'CREATE TABLE "tbl19"' not in system  # unrelated tables are cut to stay under the cap


# ── daily (TPD/RPD) 429 handling in the runner ───────────────────────────────

TPD = "Rate limit reached for model on tokens per day (TPD): Limit 200000"


class _Resp:
    headers: dict[str, str] = {}


class _Err(Exception):
    status_code = 429

    def __init__(self, msg: str) -> None:
        super().__init__(msg)
        self.response = _Resp()


def _clocked_pacer(**kw: Any) -> tuple[Pacer, list[float]]:
    t = [0.0]
    slept: list[float] = []

    async def sleep(s: float) -> None:
        slept.append(s)
        t[0] += s

    p = Pacer(
        rpd=1000,
        rpd_margin=0,
        clock=lambda: t[0],
        utcnow=lambda: datetime(2026, 11, 14, 10, tzinfo=UTC) + timedelta(seconds=t[0]),
        sleep=sleep,
        **kw,
    )
    return p, slept


class Hit429:
    """Calls the pacer like the LLM layer does; the first ``fail`` calls get a daily 429."""

    name = "hit429"

    def __init__(self, pacer: Pacer, fail: int) -> None:
        self.pacer, self.fail = pacer, fail
        self.calls: list[int] = []

    async def predict(self, q: Question, use_evidence: bool) -> Prediction:
        self.calls.append(q.question_id)
        try:
            await self.pacer.acquire("m", 1)
        except DailyBudgetExhausted as exc:
            return Prediction(status="infra_error", llm_error=str(exc))
        if self.fail > 0:
            self.fail -= 1
            self.pacer.note_rate_limited("m", _Err(TPD))
            return Prediction(status="infra_error", llm_error="429 " + TPD)
        self.pacer.settle("m", 1, 1)
        return Prediction(status="answered", sql=GOLD_A, decision="sql")


async def _noop_sleep(_s: float) -> None:
    return None


async def test_one_daily_429_then_success_continues_the_run(tmp_path: Path) -> None:
    pacer, slept = _clocked_pacer()
    state = RunState()
    cfg = RunConfig(
        checkpoint=tmp_path / "r.jsonl",
        ves_repeats=0,
        concurrency=1,
        pacer=pacer,
        budget_models=("m",),
        state=state,
    )
    v = Hit429(pacer, fail=1)
    recs = await run_questions(QS, v, MemSql(), TABLE_COLUMNS, cfg, sleep=_noop_sleep)
    assert len(recs) == len(QS) and state.stopped_reason is None
    assert v.calls[:2] == [1, 1]  # the SAME question was retried
    assert sum(slept) >= 300  # waited on the mocked clock, never for real
    assert max(slept) <= 30  # in slices toward a wall-clock deadline
    assert pacer.daily_strikes("m") == 0


async def test_consecutive_daily_429s_stop_the_run_as_resumable(tmp_path: Path) -> None:
    pacer, _ = _clocked_pacer(daily_max_strikes=4, daily_min_span_s=0)
    state = RunState()
    cfg = RunConfig(
        checkpoint=tmp_path / "r.jsonl",
        ves_repeats=0,
        concurrency=1,
        pacer=pacer,
        budget_models=("m",),
        state=state,
    )
    v = Hit429(pacer, fail=100)
    await run_questions(QS, v, MemSql(), TABLE_COLUMNS, cfg, sleep=_noop_sleep)
    assert state.stopped_reason and "daily request budget" in state.stopped_reason
    assert v.calls[:5] == [1, 1, 1, 1, 1]  # 4 strikes on question 1, then the stop
    assert not load_checkpoint(cfg.checkpoint)  # nothing recorded as done: resumable
    assert state.skipped == len(QS)


def test_cli_reset_usage_clears_exhausted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json
    from types import SimpleNamespace

    from typer.testing import CliRunner

    from nl2sql.eval import cli as cli_mod

    usage = tmp_path / "u.json"
    now = datetime.now(UTC)
    entry = {"requests": 3, "rate_limited": 1, "exhausted": True, "exhausted_at": now.isoformat()}
    usage.write_text(json.dumps({"date": now.strftime("%Y-%m-%d"), "models": {"m": entry}}))
    assert Pacer(state_path=usage).remaining_requests("m") == 0

    fake_settings = SimpleNamespace(
        llm_provider="groq",
        llm_pacing_enabled=True,
        llm_tpm_limit=8000,
        llm_rpd_limit=1000,
        bird_data_dir=str(tmp_path),
        profile_dir=str(tmp_path),
        planner_model="m",
    )
    monkeypatch.setattr(cli_mod, "get_settings", lambda: fake_settings)
    monkeypatch.setattr(cli_mod, "load_questions", lambda *a, **k: QS)
    monkeypatch.setattr(cli_mod, "load_table_columns", lambda *a, **k: {})

    def stop(*a: Any, **k: Any) -> None:
        raise RuntimeError("stop after the pacer is set up")

    monkeypatch.setattr(cli_mod.DatabaseRegistry, "for_bird", stop)
    args = ["run", "--reset-usage", "--usage-file", str(usage), "--out-dir", str(tmp_path)]
    out = CliRunner().invoke(cli_mod.app, args)
    assert "--reset-usage: cleared exhausted state for ['m']" in out.output
    assert json.loads(usage.read_text())["models"]["m"]["exhausted"] is False
