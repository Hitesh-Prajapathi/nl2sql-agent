"""Async evaluation runner: bounded concurrency, resumable JSONL checkpoint, scoring.

Rate limits: with a ``Pacer`` in the config the runner stops starting questions when the daily
request budget of the models in ``budget_models`` cannot cover another question (and stops on
the first question that hits the budget mid-flight). Stopping is clean and resumable: finished
questions are in the JSONL checkpoint, and rerunning the same command continues from there.
"""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from nl2sql.eval.dataset import Question
from nl2sql.eval.metrics import (
    Row,
    execution_match,
    gold_links,
    parse_predicted_links,
    set_precision_recall,
    soft_f1,
    ves_reward,
    ves_time_ratio,
)
from nl2sql.eval.sqlrunner import SqlOutcome, SqlRunner
from nl2sql.eval.variants import Prediction, Variant, is_permanent_llm_error, is_rate_limit
from nl2sql.llm.pacer import DAILY_MARKER, Pacer

log = structlog.get_logger()

SCORED_STATUSES = ("answered", "needs_clarification", "refused", "failed")


class CheckpointMismatch(RuntimeError):
    """The checkpoint was written by a different configuration (model, prompts, cap, ...)."""


@dataclass
class RunState:
    """Filled in by ``run_questions`` so the caller can report how the run ended."""

    stopped_reason: str | None = None
    skipped: int = 0  # questions not started because the run stopped


@dataclass
class RunConfig:
    variant: str = "full"
    use_evidence: bool = True
    concurrency: int = 4
    checkpoint: Path | None = None
    ves_repeats: int = 3
    sql_timeout_s: float = 30.0
    max_infra_retries: int = 3
    infra_backoff_s: float = 5.0
    infra_backoff_cap_s: float = 120.0
    extra: dict[str, Any] = field(default_factory=dict)
    # Daily-budget stop (None = no pacing, e.g. tests and the fake provider)
    pacer: Pacer | None = None
    budget_models: tuple[str, ...] = ()
    calls_per_question: int = 1  # worst-case logical LLM calls one question can make
    # Identity of this run; a checkpoint written under a different one is refused.
    fingerprint: dict[str, Any] | None = None
    state: RunState = field(default_factory=RunState)


def load_checkpoint(path: Path | None) -> dict[int, dict[str, Any]]:
    """question_id -> record for finished questions (infra errors are NOT finished)."""
    done: dict[int, dict[str, Any]] = {}
    if path is None or not path.exists():
        return done
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue  # torn last line from an interrupted run
        if rec.get("status") in SCORED_STATUSES:
            done[int(rec["question_id"])] = rec
    return done


def prepare_checkpoint(path: Path | None, fingerprint: dict[str, Any] | None) -> None:
    """Write the identity header of a new checkpoint; refuse to resume a different configuration."""
    if path is None or fingerprint is None:
        return
    if path.exists() and path.stat().st_size > 0:
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                head = json.loads(line)
            except ValueError:
                break
            if isinstance(head, dict) and "_meta" in head:
                old = head["_meta"]
                keys = set(old) | set(fingerprint)
                diff = sorted(k for k in keys if old.get(k) != fingerprint.get(k))
                if diff:
                    raise CheckpointMismatch(
                        f"{path} was written with a different configuration (differs in: "
                        f"{', '.join(diff)}). Delete it (or pass --fresh) to start over."
                    )
            break
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"_meta": fingerprint}, default=str) + "\n")


async def score_prediction(
    q: Question,
    pred: Prediction,
    sql_runner: SqlRunner,
    table_columns: dict[str, dict[str, list[str]]],
    cfg: RunConfig,
    timing_lock: asyncio.Lock,
) -> dict[str, Any]:
    """Compute EX / Soft F1 / R-VES reward and schema-linking scores for one prediction."""
    rec: dict[str, Any] = {
        "question_id": q.question_id,
        "db_id": q.db_id,
        "difficulty": q.difficulty,
        "question": q.question,
        "evidence": q.evidence,
        "gold_sql": q.gold_sql,
        "status": pred.status,
        "pred_sql": pred.sql,
        "decision": pred.decision,
        "failed_attempts": pred.failed_attempts,
        "attempts_total": 1 + pred.failed_attempts if pred.decision == "sql" else 0,
        "latency_s": round(pred.latency_s, 3),
        "usage": pred.usage,
        "llm_calls": pred.llm_calls,
        "llm_error": pred.llm_error,
        "agent_error": pred.error,
        "relevant_tables": pred.relevant_tables,
        "exec_row_count": pred.exec_row_count,
        "exec_truncated": pred.exec_truncated,
        "node_timings_ms": pred.node_timings_ms,
        "ex": 0,
        "soft_f1": 0.0,
        "ves_reward": 0.0,
        "ves_time_ratio": None,
        "pred_error": None,
        "gold_error": None,
        "n_pred_rows": None,
        "n_gold_rows": None,
    }
    if pred.status == "infra_error":
        return rec

    gold = await sql_runner.run(q.db_id, q.gold_sql, cfg.sql_timeout_s)
    if not gold.ok:
        rec["gold_error"] = gold.error
    else:
        rec["n_gold_rows"] = len(gold.rows or [])

    pred_out: SqlOutcome | None = None
    if pred.status == "answered" and pred.sql:
        pred_out = await sql_runner.run(q.db_id, pred.sql, cfg.sql_timeout_s)
        if not pred_out.ok:
            rec["pred_error"] = pred_out.error
        else:
            rec["n_pred_rows"] = len(pred_out.rows or [])

    if gold.ok and pred_out is not None and pred_out.ok:
        g_rows: list[Row] = gold.rows or []
        p_rows: list[Row] = pred_out.rows or []
        rec["ex"] = int(execution_match(p_rows, g_rows))
        rec["soft_f1"] = round(soft_f1(p_rows, g_rows), 6)
        if rec["ex"] and cfg.ves_repeats > 0:
            rec.update(await _ves(q, pred.sql, sql_runner, cfg, timing_lock))

    rec.update(_schema_linking(q, pred, table_columns))
    return rec


async def _ves(
    q: Question, pred_sql: str, sql_runner: SqlRunner, cfg: RunConfig, lock: asyncio.Lock
) -> dict[str, Any]:
    gold_t: list[float] = []
    pred_t: list[float] = []
    async with lock:  # one timing measurement at a time, to limit self-inflicted noise
        for _ in range(cfg.ves_repeats):
            g = await sql_runner.run(q.db_id, q.gold_sql, cfg.sql_timeout_s)
            p = await sql_runner.run(q.db_id, pred_sql, cfg.sql_timeout_s)
            if not (g.ok and p.ok):
                return {"ves_reward": 0.0, "ves_time_ratio": None}
            gold_t.append(g.elapsed_s)
            pred_t.append(p.elapsed_s)
    ratio = ves_time_ratio(gold_t, pred_t)
    return {"ves_reward": ves_reward(ratio), "ves_time_ratio": round(ratio, 4)}


def _schema_linking(
    q: Question, pred: Prediction, table_columns: dict[str, dict[str, list[str]]]
) -> dict[str, Any]:
    schema = table_columns.get(q.db_id)
    links = gold_links(q.gold_sql, schema)
    out: dict[str, Any] = {
        "link_gold_parsed": links is not None,
        "link_table_precision": None,
        "link_table_recall": None,
        "link_column_precision": None,
        "link_column_recall": None,
    }
    if links is None or pred.decision != "sql" or not pred.relevant_tables:
        return out
    p_tables, p_cols = parse_predicted_links(pred.relevant_tables, set(schema or {}))
    out["link_table_precision"], out["link_table_recall"] = set_precision_recall(
        p_tables, links.tables
    )
    out["link_column_precision"], out["link_column_recall"] = set_precision_recall(
        p_cols, links.columns
    )
    return out


async def run_questions(
    questions: Sequence[Question],
    variant: Variant,
    sql_runner: SqlRunner,
    table_columns: dict[str, dict[str, list[str]]],
    cfg: RunConfig,
    on_progress: Callable[[dict[str, Any], int, int], None] | None = None,
    sleep: Callable[[float], Any] = asyncio.sleep,
) -> list[dict[str, Any]]:
    """Run (resuming from the checkpoint) and return one record per question, in input order."""
    prepare_checkpoint(cfg.checkpoint, cfg.fingerprint)
    done = load_checkpoint(cfg.checkpoint)
    todo = [q for q in questions if q.question_id not in done]
    sem = asyncio.Semaphore(max(1, cfg.concurrency))
    timing_lock = asyncio.Lock()
    write_lock = asyncio.Lock()
    results: dict[int, dict[str, Any]] = {qid: rec for qid, rec in done.items()}
    counter = {"n": len(done), "inflight": 0}
    total = len(questions)
    state = cfg.state

    def budget_blocks() -> str | None:
        if cfg.pacer is None or not cfg.budget_models:
            return None
        remaining = min(cfg.pacer.remaining_requests(m) for m in cfg.budget_models)
        needed = cfg.calls_per_question * counter["inflight"]
        if remaining < needed:
            return (
                f"daily request budget nearly exhausted ({remaining} requests left for "
                f"{', '.join(cfg.budget_models)}; {needed} needed for the questions in flight)"
            )
        return None

    async def one(q: Question) -> None:
        async with sem:
            if state.stopped_reason is not None:
                state.skipped += 1
                return
            counter["inflight"] += 1
            try:
                if (why := budget_blocks()) is not None:
                    state.stopped_reason = why
                    state.skipped += 1
                    return
                pred = await _predict_with_retry(variant, q, cfg, sleep)
                if pred.status == "infra_error" and DAILY_MARKER in pred.llm_error.lower():
                    state.stopped_reason = state.stopped_reason or pred.llm_error[:200]
                    state.skipped += 1  # not finished: the resume reruns it
                    return
                rec = await score_prediction(q, pred, sql_runner, table_columns, cfg, timing_lock)
            finally:
                counter["inflight"] -= 1
        results[q.question_id] = rec
        async with write_lock:
            counter["n"] += 1
            # Infra errors are written for visibility but are not "done": a resume retries them.
            if cfg.checkpoint is not None:
                cfg.checkpoint.parent.mkdir(parents=True, exist_ok=True)
                with cfg.checkpoint.open("a") as f:
                    f.write(json.dumps(rec, default=str) + "\n")
        if on_progress:
            on_progress(rec, counter["n"], total)

    await asyncio.gather(*(one(q) for q in todo))
    return [results[q.question_id] for q in questions if q.question_id in results]


def _daily_429_pending(cfg: RunConfig) -> bool:
    if cfg.pacer is None:
        return False
    return any(cfg.pacer.daily_strikes(m) > 0 for m in cfg.budget_models)


async def _predict_with_retry(
    variant: Variant,
    q: Question,
    cfg: RunConfig,
    sleep: Callable[[float], Any],
) -> Prediction:
    """Retry infrastructure failures (LLM 429 / outage) with jittered exponential backoff.

    Backoff sleeps happen between attempts and are NOT included in ``latency_s`` (each attempt
    times itself; only the final attempt's latency is kept).
    """
    pred = await variant.predict(q, cfg.use_evidence)
    n = 0
    while n < cfg.max_infra_retries:
        if (
            pred.status != "infra_error"
            or is_permanent_llm_error(pred.llm_error)
            or DAILY_MARKER in pred.llm_error.lower()
        ):
            break
        text = pred.llm_error
        if _daily_429_pending(cfg):
            # The pacer is backing off after a daily 429 (it sleeps in ``acquire``); retry the
            # SAME question without spending a retry until it succeeds or the pacer gives up.
            log.warning("eval_daily_429_retry", question_id=q.question_id, error=text[:120])
            pred = await variant.predict(q, cfg.use_evidence)
            continue
        base = cfg.infra_backoff_s * (4 if is_rate_limit(text) else 1)
        delay = min(cfg.infra_backoff_cap_s, base * 2**n)
        delay = random.uniform(delay / 2, delay)  # noqa: S311 - jitter, not crypto
        log.warning(
            "eval_infra_retry",
            question_id=q.question_id,
            attempt=n + 1,
            delay_s=round(delay, 1),
            error=text[:120],
        )
        await sleep(delay)
        pred = await variant.predict(q, cfg.use_evidence)
        n += 1
    return pred
