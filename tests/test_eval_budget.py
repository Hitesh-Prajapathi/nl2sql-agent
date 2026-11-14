"""Daily-budget stop, resumable checkpoint identity, sampling and the small naive variant."""

from __future__ import annotations

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
from nl2sql.llm.pacer import DAILY_MARKER, Pacer
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
