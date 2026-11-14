"""Metrics on hand-built pairs (EX, Soft F1, R-VES, schema linking, stats, summary)."""

from __future__ import annotations

import math
from typing import Any

import pytest

from nl2sql.eval.metrics import (
    execution_match,
    gold_links,
    normalize_rows,
    parse_predicted_links,
    set_precision_recall,
    soft_f1,
    ves_reward,
    ves_score,
    ves_time_ratio,
)
from nl2sql.eval.stats import bootstrap_ci, paired_bootstrap_diff, percentile
from nl2sql.eval.summary import summarize

# ── EX ────────────────────────────────────────────────────────────────────────


def test_ex_is_set_comparison() -> None:
    assert execution_match([(1,), (2,)], [(2,), (1,), (1,)])  # order and duplicates ignored
    assert not execution_match([(1,)], [(1,), (2,)])
    assert not execution_match([(1, "a")], [(1, "b")])
    assert execution_match([], [])
    assert not execution_match([], [(1,)])


def test_normalize_rows_makes_unhashable_values_comparable() -> None:
    rows = normalize_rows([([1, 2], {"a": 1}), ([1, 2], {"a": 1})])
    assert len(set(rows)) == 1


# ── Soft F1 ───────────────────────────────────────────────────────────────────


def test_soft_f1_identical_and_empty() -> None:
    assert soft_f1([(1, "a")], [(1, "a")]) == 1.0
    assert soft_f1([], []) == 1.0
    assert soft_f1([], [(1,)]) == 0.0
    assert soft_f1([(1,)], [(2,)]) == 0.0


def test_soft_f1_partial_row_overlap() -> None:
    # one of two columns matches: tp=0.5, fp=0.5, fn=0.5 -> P=R=0.5 -> F1=0.5
    assert soft_f1([(1, "a")], [(1, "b")]) == pytest.approx(0.5)


def test_soft_f1_extra_predicted_row() -> None:
    # tp=1, fp=1 (extra row), fn=0 -> P=0.5, R=1 -> F1=2/3
    assert soft_f1([(1,), (2,)], [(1,)]) == pytest.approx(2 / 3)


def test_soft_f1_missing_predicted_row() -> None:
    # tp=1, fp=0, fn=1 -> P=1, R=0.5 -> F1=2/3
    assert soft_f1([(1,)], [(1,), (2,)]) == pytest.approx(2 / 3)


def test_soft_f1_dedups_before_positional_pairing() -> None:
    assert soft_f1([(1,), (1,), (2,)], [(1,), (2,)]) == 1.0


# ── R-VES ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("ratio", "reward"),
    [
        (0.0, 0.0),
        (2.0, 1.25),
        (5.0, 1.25),
        (1.0, 1.0),
        (1.99, 1.0),
        (0.5, 0.75),
        (0.99, 0.75),
        (0.25, 0.5),
        (0.49, 0.5),
        (0.1, 0.25),
    ],
)
def test_ves_reward_bands(ratio: float, reward: float) -> None:
    assert ves_reward(ratio) == reward


def test_ves_time_ratio_mean_and_outlier_filter() -> None:
    assert ves_time_ratio([2.0, 2.0], [1.0, 1.0]) == pytest.approx(2.0)  # zero spread kept
    gold = [1.0] * 20 + [1000.0]
    pred = [1.0] * 21
    # the 1000x spike lies outside mean +/- 3 sigma and is dropped
    assert ves_time_ratio(gold, pred) == pytest.approx(1.0)
    assert ves_time_ratio([], []) == 0.0


def test_ves_score_aggregation() -> None:
    assert ves_score([1.25, 0.0]) == pytest.approx(math.sqrt(1.25) * 100 / 2)
    assert ves_score([]) == 0.0


# ── Schema linking ────────────────────────────────────────────────────────────

SCHEMA = {"t1": ["a", "id"], "t2": ["b", "id", "c"]}


def test_gold_links_resolves_aliases_and_unqualified_columns() -> None:
    sql = "SELECT T1.a, b FROM t1 AS T1 JOIN t2 ON T1.id = t2.id WHERE t2.c = 1"
    links = gold_links(sql, SCHEMA)
    assert links is not None
    assert links.tables == {"t1", "t2"}
    assert links.columns == {"t1.a", "t2.b", "t1.id", "t2.id", "t2.c"}
    assert links.unresolved == 0


def test_gold_links_counts_unresolvable_references() -> None:
    links = gold_links("SELECT x.q FROM (SELECT a FROM t1) AS x", SCHEMA)
    assert links is not None
    assert links.tables == {"t1"}
    assert links.unresolved == 1  # x.q belongs to a derived table


def test_gold_links_cte_not_a_base_table_and_unparsable() -> None:
    links = gold_links("WITH c AS (SELECT a FROM t1) SELECT a FROM c", SCHEMA)
    assert links is not None and links.tables == {"t1"}
    assert gold_links("SELEC FROM WHERE (", SCHEMA) is None


def test_parse_predicted_links() -> None:
    tables, cols = parse_predicted_links(
        ['"T1".A', "t2.b", "public.t2.c", "t2", "unknown", ""], {"t1", "t2"}
    )
    assert tables == {"t1", "t2"}
    assert cols == {"t1.a", "t2.b", "t2.c"}


def test_precision_recall() -> None:
    p, r = set_precision_recall({"t1", "t3"}, {"t1", "t2"})
    assert (p, r) == (0.5, 0.5)
    assert set_precision_recall(set(), {"t1"}) == (None, 0.0)
    assert set_precision_recall({"t1"}, set()) == (0.0, None)


# ── Stats ─────────────────────────────────────────────────────────────────────


def test_percentile() -> None:
    xs = [1.0, 2.0, 3.0, 4.0]
    assert percentile(xs, 50) == 2.5
    assert percentile(xs, 0) == 1.0 and percentile(xs, 100) == 4.0
    assert percentile([7.0], 95) == 7.0
    assert percentile([], 50) is None


def test_bootstrap_ci_brackets_the_mean_and_is_reproducible() -> None:
    vals = [1.0] * 50 + [0.0] * 50
    ci = bootstrap_ci(vals, seed=1)
    assert ci is not None and ci[0] < 0.5 < ci[1]
    assert ci[0] > 0.35 and ci[1] < 0.65
    assert bootstrap_ci(vals, seed=1) == ci
    assert bootstrap_ci([]) is None
    assert bootstrap_ci([1.0, 1.0, 1.0]) == (1.0, 1.0)


def test_paired_bootstrap_diff() -> None:
    d = paired_bootstrap_diff([1, 1, 1, 0], [0, 0, 0, 0])
    assert d is not None and d[0] == 0.75 and d[1] <= 0.75 <= d[2]
    assert paired_bootstrap_diff([1], [1, 0]) is None


# ── Summary / cost ────────────────────────────────────────────────────────────


def _rec(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "question_id": 1,
        "db_id": "db1",
        "difficulty": "simple",
        "status": "answered",
        "ex": 1,
        "soft_f1": 1.0,
        "ves_reward": 1.0,
        "pred_error": None,
        "gold_error": None,
        "attempts_total": 1,
        "failed_attempts": 0,
        "latency_s": 1.0,
        "usage": {},
        "link_gold_parsed": True,
    }
    base.update(kw)
    return base


def test_summary_counts_non_answers_as_wrong_and_excludes_infra() -> None:
    recs = [
        _rec(question_id=1),
        _rec(question_id=2, ex=0, soft_f1=0.0, ves_reward=0.0, failed_attempts=1, attempts_total=2),
        _rec(
            question_id=3,
            status="needs_clarification",
            ex=0,
            soft_f1=0.0,
            ves_reward=0.0,
            attempts_total=0,
            difficulty="moderate",
        ),
        _rec(question_id=4, status="refused", ex=0, soft_f1=0.0, ves_reward=0.0, attempts_total=0),
        _rec(
            question_id=5,
            status="failed",
            ex=0,
            soft_f1=0.0,
            ves_reward=0.0,
            failed_attempts=3,
            attempts_total=4,
            pred_error=None,
        ),
        _rec(question_id=6, status="infra_error", ex=0),
    ]
    s = summarize(recs)
    assert (s["n_total"], s["n_scored"], s["n_infra_error"]) == (6, 5, 1)
    assert s["infra_error_ids"] == [6]
    assert s["ex"]["value"] == 20.0  # 1 of 5 scored
    assert s["rates"]["clarify"]["value"] == 20.0
    assert s["rates"]["refuse"]["value"] == 20.0
    assert s["rates"]["failed"]["value"] == 20.0
    assert s["by_difficulty"]["moderate"]["n"] == 1
    # attempts over questions that produced SQL: (1 + 2 + 4) / 3
    assert s["mean_attempts"] == pytest.approx(7 / 3, abs=1e-3)
    # failed_attempts>=1: questions 2 and 5; none recovered
    assert s["retry_recovery"] == {
        "n_with_failed_attempt": 2,
        "recovered": 0,
        "rate": 0.0,
        "note": s["retry_recovery"]["note"],
    }
    assert s["r_ves"]["value"] == 20.0


def test_summary_retry_recovery_and_tokens_without_cost() -> None:
    usage = {
        "plan_sql": {
            "m": {"calls": 1, "cached_calls": 0, "input_tokens": 1000, "output_tokens": 100}
        }
    }
    recs = [
        _rec(question_id=1, failed_attempts=1, attempts_total=2, usage=usage),
        _rec(question_id=2, usage=usage),
    ]
    s = summarize(recs)
    assert s["retry_recovery"]["rate"] == 1.0
    assert s["tokens_by_node"]["plan_sql"]["m"]["input_tokens"] == 2000
    assert s["tokens_total"]["output"] == 200
    assert "cost_usd" not in s and "cost_note" not in s
    assert s["latency_s"]["p50"] == 1.0


def test_model_usage_splits_planner_and_fallback_and_books_tokens_to_the_answering_model() -> None:
    def call(model: str, leg: str, i: int, o: int) -> dict[str, Any]:
        return {
            "node": "plan_sql",
            "role": "planner",
            "leg": leg,
            "model": model,
            "input_tokens": i,
            "output_tokens": o,
            "cached": False,
        }

    recs = [
        _rec(question_id=1, llm_calls=[call("big", "primary", 1000, 100)]),
        _rec(
            question_id=2,
            llm_calls=[call("small", "fallback", 800, 50), call("big", "primary", 500, 20)],
        ),
    ]
    mu = summarize(recs)["model_usage"]
    assert mu["by_model"]["big"]["input_tokens"] == 1500 and mu["by_model"]["big"]["calls"] == 2
    assert mu["by_model"]["small"] == {
        "calls": 1,
        "cached_calls": 0,
        "input_tokens": 800,
        "output_tokens": 50,
    }
    assert mu["calls_by_role_and_leg"]["planner"] == {"primary": 2, "fallback": 1}
    assert mu["fallback_calls"] == 1 and mu["fallback_share"] == pytest.approx(1 / 3, abs=1e-3)
