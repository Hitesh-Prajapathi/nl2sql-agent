"""Aggregate per-question records into the headline summary (with bootstrap CIs)."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence
from typing import Any

from nl2sql.eval.stats import bootstrap_ci, mean, percentile

DIFFICULTIES = ("simple", "moderate", "challenging")


def _pct(values: Sequence[float]) -> dict[str, Any]:
    """Mean x100 with a 95% bootstrap CI."""
    if not values:
        return {"value": None, "ci95": None, "n": 0}
    ci = bootstrap_ci(values)
    m = sum(values) / len(values)
    return {
        "value": round(m * 100, 2),
        "ci95": [round(ci[0] * 100, 2), round(ci[1] * 100, 2)] if ci else None,
        "n": len(values),
    }


def _group(records: list[dict[str, Any]], key: str) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in records:
        groups[str(r[key])].append(r)
    out: dict[str, Any] = {}
    for name in sorted(groups):
        rs = groups[name]
        out[name] = {
            "n": len(rs),
            "ex": round(100 * sum(r["ex"] for r in rs) / len(rs), 2),
            "soft_f1": round(100 * sum(r["soft_f1"] for r in rs) / len(rs), 2),
            "r_ves": round(sum(math.sqrt(r["ves_reward"]) * 100 for r in rs) / len(rs), 2),
        }
    return out


def _avg(values: list[float]) -> float | None:
    m = mean(values)
    return None if m is None else round(m, 4)


def model_usage(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Calls and tokens per model that actually answered, and the planner / fallback split."""
    by_model: dict[str, dict[str, int]] = {}
    by_role: dict[str, dict[str, int]] = {}
    for r in records:
        for c in r.get("llm_calls") or []:
            m = by_model.setdefault(
                c["model"], {"calls": 0, "cached_calls": 0, "input_tokens": 0, "output_tokens": 0}
            )
            m["calls"] += 1
            m["cached_calls"] += int(bool(c.get("cached")))
            m["input_tokens"] += int(c.get("input_tokens", 0))
            m["output_tokens"] += int(c.get("output_tokens", 0))
            role = by_role.setdefault(c.get("role") or "unknown", {"primary": 0, "fallback": 0})
            role[c.get("leg", "primary")] = role.get(c.get("leg", "primary"), 0) + 1
    total = sum(v["calls"] for v in by_model.values())
    fallback = sum(v.get("fallback", 0) for v in by_role.values())
    return {
        "by_model": by_model,
        "calls_by_role_and_leg": by_role,
        "fallback_calls": fallback,
        "fallback_share": round(fallback / total, 4) if total else None,
        "note": "tokens are booked to the model that produced the response (planner or fallback)",
    }


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Questions with ``infra_error`` are excluded from every rate and listed separately."""
    infra = [r for r in records if r["status"] == "infra_error"]
    scored = [r for r in records if r["status"] != "infra_error"]
    n = len(scored)
    summary: dict[str, Any] = {
        "n_total": len(records),
        "n_scored": n,
        "n_infra_error": len(infra),
        "infra_error_ids": sorted(r["question_id"] for r in infra),
    }
    if n == 0:
        return summary

    summary["ex"] = _pct([float(r["ex"]) for r in scored])
    summary["soft_f1"] = _pct([float(r["soft_f1"]) for r in scored])
    ves_per_q = [math.sqrt(r["ves_reward"]) for r in scored]
    summary["r_ves"] = _pct(ves_per_q)
    summary["by_difficulty"] = {k: v for k, v in _group(scored, "difficulty").items()}
    summary["by_db_id"] = _group(scored, "db_id")

    # clarify / refuse / failure all count as wrong; reported on their own as well
    def rate(pred: Any) -> dict[str, Any]:
        return _pct([1.0 if pred(r) else 0.0 for r in scored])

    summary["rates"] = {
        "clarify": rate(lambda r: r["status"] == "needs_clarification"),
        "refuse": rate(lambda r: r["status"] == "refused"),
        "failed": rate(lambda r: r["status"] == "failed"),
        "executed_but_error": rate(lambda r: r["status"] == "answered" and r["pred_error"]),
        "answered_wrong": rate(
            lambda r: r["status"] == "answered" and not r["pred_error"] and not r["ex"]
        ),
        "gold_error": rate(lambda r: bool(r["gold_error"])),
    }

    sql_runs = [r for r in scored if r["attempts_total"] > 0]
    summary["mean_attempts"] = _avg([float(r["attempts_total"]) for r in sql_runs])
    needed_retry = [r for r in sql_runs if r["failed_attempts"] >= 1]
    summary["retry_recovery"] = {
        "n_with_failed_attempt": len(needed_retry),
        "recovered": sum(1 for r in needed_retry if r["ex"]),
        "rate": (
            round(sum(1 for r in needed_retry if r["ex"]) / len(needed_retry), 4)
            if needed_retry
            else None
        ),
        "note": "correct after >=1 failed attempt / questions with >=1 failed attempt",
    }

    def link(field: str) -> dict[str, Any]:
        vals = [r[field] for r in scored if r.get(field) is not None]
        return {"mean": _avg(vals), "n": len(vals)}

    summary["schema_linking"] = {
        "table_recall": link("link_table_recall"),
        "table_precision": link("link_table_precision"),
        "column_recall": link("link_column_recall"),
        "column_precision": link("link_column_precision"),
        "gold_unparsable": sum(1 for r in scored if not r.get("link_gold_parsed", True)),
        "note": "macro-average over questions; gold tables/columns extracted with sqlglot",
    }

    # tokens per node (and per model), summed over scored questions
    tokens: dict[str, dict[str, dict[str, int]]] = {}
    for r in scored:
        for node, per_model in (r.get("usage") or {}).items():
            for model, u in per_model.items():
                t = tokens.setdefault(node, {}).setdefault(
                    model, {"calls": 0, "cached_calls": 0, "input_tokens": 0, "output_tokens": 0}
                )
                for k in t:
                    t[k] += u.get(k, 0)
    summary["tokens_by_node"] = tokens
    summary["tokens_total"] = {
        "input": sum(u["input_tokens"] for m in tokens.values() for u in m.values()),
        "output": sum(u["output_tokens"] for m in tokens.values() for u in m.values()),
        "llm_calls": sum(u["calls"] for m in tokens.values() for u in m.values()),
        "cached_calls": sum(u["cached_calls"] for m in tokens.values() for u in m.values()),
    }
    summary["model_usage"] = model_usage(scored)
    summary["tokens_note"] = "tokens counted for every call including cache hits (as if uncached)"

    lat = [float(r["latency_s"]) for r in scored if r["latency_s"] is not None]
    summary["latency_s"] = {
        "p50": _round(percentile(lat, 50)),
        "p95": _round(percentile(lat, 95)),
        "mean": _round(mean(lat)),
        "note": "agent wall time per question (excludes scoring and infra-retry backoff sleeps; "
        "includes cache-hit calls, so cached re-runs understate it)",
    }
    summary["truncated_results"] = sum(1 for r in scored if r.get("exec_truncated"))
    return summary


def _round(x: float | None) -> float | None:
    return None if x is None else round(x, 3)
