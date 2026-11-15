#!/usr/bin/env python
"""Clarification eval: does the agent ask for clarification exactly when it should?

Sends each of the 100 questions in questions.yaml (50 ambiguous, 50 clear; demo e-commerce
schema, database nl2sql_assignment) through the full agent graph, one fresh single-turn
conversation per question. "Asked for clarification" is mechanical: final status ==
"needs_clarification".

Positive class = ambiguous. Reported, each with a 95% bootstrap CI:
  precision = TP / (TP + FP)       recall = TP / n_ambiguous      F1
  false-clarify rate = FP / n_clear  (over-asking, the cost of the behaviour)
Rates over a single class (recall, false-clarify) use nl2sql.eval.stats.bootstrap_ci;
precision and F1 depend on both classes so they use a stratified percentile bootstrap
(resample ambiguous and clear items separately, keeping class sizes). A resample with zero
predicted positives has no precision: it is skipped for precision, F1 counts it as 0.

Infrastructure errors (llm_error / exceptions) are excluded from every denominator, reported
as n_errors, and retried on resume.

Refuses to use a real provider unless --confirm-real-llm is passed.

Usage
  PYTHONPATH=$PWD/src uv run python evalsets/clarification/run_clarification_eval.py \
      --label v1 --confirm-real-llm [--limit 10] [--ids amb-001,clr-003]
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import random
import sys
import time
import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from nl2sql.eval.stats import bootstrap_ci, percentile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
QUESTIONS = HERE / "questions.yaml"

AskFn = Callable[[str, str], Awaitable[dict[str, Any]]]


def load_items(path: Path = QUESTIONS) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = yaml.safe_load(path.read_text())
    ids = [i["id"] for i in items]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate ids")
    for i in items:
        if i["label"] not in ("ambiguous", "clear"):
            raise ValueError(f"bad label on {i['id']}")
    return items


def dataset_sha256(path: Path = QUESTIONS) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ── Running ──────────────────────────────────────────────────────────────────


async def run_item(item: dict[str, Any], ask: AskFn) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "id": item["id"],
        "label": item["label"],
        "ambiguity_type": item.get("ambiguity_type"),
        "difficulty": item.get("difficulty"),
        "question": item["question"],
        "error": "",
    }
    t0 = time.perf_counter()
    try:
        res = await ask(item["question"], f"clar-{item['id']}-{uuid.uuid4().hex[:6]}")
    except Exception as exc:
        rec["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        rec["wall_s"] = round(time.perf_counter() - t0, 2)
        return rec
    rec["wall_s"] = round(time.perf_counter() - t0, 2)
    rec["status"] = str(res.get("status", "failed"))
    rec["decision"] = res.get("decision")
    if res.get("llm_error"):
        rec["error"] = f"llm_error: {str(res['llm_error'])[:200]}"
        return rec
    rec["asked"] = rec["status"] == "needs_clarification"
    rec["clarification"] = str(res.get("clarification_question") or "")[:400]
    rec["sql"] = str(res.get("final_sql") or "")[:300]
    rec["usage"] = res.get("_usage", {})
    return rec


async def run_suite(
    items: Sequence[dict[str, Any]],
    ask: AskFn,
    *,
    concurrency: int = 1,
    checkpoint: Path | None = None,
    done: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    done = done or {}
    sem = asyncio.Semaphore(max(1, concurrency))
    out = dict(done)

    async def one(item: dict[str, Any]) -> None:
        if item["id"] in done:
            return
        async with sem:
            rec = await run_item(item, ask)
        out[item["id"]] = rec
        if checkpoint is not None:
            with checkpoint.open("a") as f:
                f.write(json.dumps(rec, default=str) + "\n")

    await asyncio.gather(*(one(i) for i in items))
    return [out[i["id"]] for i in items if i["id"] in out]


def load_checkpoint(path: Path) -> dict[str, dict[str, Any]]:
    done: dict[str, dict[str, Any]] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip():
                rec = json.loads(line)
                if not rec.get("error"):
                    done[rec["id"]] = rec
    return done


# ── Metrics ──────────────────────────────────────────────────────────────────


def confusion(records: Sequence[dict[str, Any]]) -> dict[str, int]:
    valid = [r for r in records if not r.get("error")]
    tp = sum(1 for r in valid if r["label"] == "ambiguous" and r["asked"])
    fn = sum(1 for r in valid if r["label"] == "ambiguous" and not r["asked"])
    fp = sum(1 for r in valid if r["label"] == "clear" and r["asked"])
    tn = sum(1 for r in valid if r["label"] == "clear" and not r["asked"])
    return {"tp": tp, "fn": fn, "fp": fp, "tn": tn}


def prf(tp: int, fn: int, fp: int, tn: int) -> dict[str, float | None]:
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    if precision is None or recall is None:
        f1: float | None = 0.0 if (recall is not None and tp + fp == 0) else None
    else:
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    fcr = fp / (fp + tn) if fp + tn else None
    return {"precision": precision, "recall": recall, "f1": f1, "false_clarify_rate": fcr}


def _ci(values: list[float]) -> list[float] | None:
    if not values:
        return None
    lo, hi = percentile(values, 2.5), percentile(values, 97.5)
    return [round(lo or 0.0, 4), round(hi or 0.0, 4)]


def stratified_bootstrap(
    amb_asked: Sequence[int], clear_asked: Sequence[int], *, n_resamples: int = 2000, seed: int = 0
) -> dict[str, list[float] | None]:
    """95% percentile CIs for precision / recall / F1 / false-clarify rate.

    ``amb_asked[i]`` = 1 if ambiguous item i got a clarification; ``clear_asked`` likewise for
    the clear items.
    """
    rng = random.Random(seed)  # noqa: S311 - reproducible resampling, not crypto
    na, nc = len(amb_asked), len(clear_asked)
    if na == 0 or nc == 0:
        return {"precision": None, "recall": None, "f1": None, "false_clarify_rate": None}
    ps: list[float] = []
    rs: list[float] = []
    fs: list[float] = []
    fcs: list[float] = []
    for _ in range(n_resamples):
        tp = sum(amb_asked[rng.randrange(na)] for _ in range(na))
        fp = sum(clear_asked[rng.randrange(nc)] for _ in range(nc))
        m = prf(tp, na - tp, fp, nc - fp)
        if m["precision"] is not None:
            ps.append(m["precision"])
        rs.append(m["recall"] or 0.0)
        fs.append(m["f1"] or 0.0)
        fcs.append(m["false_clarify_rate"] or 0.0)
    return {
        "precision": _ci(ps),
        "recall": _ci(rs),
        "f1": _ci(fs),
        "false_clarify_rate": _ci(fcs),
    }


def summarize(records: Sequence[dict[str, Any]]) -> dict[str, Any]:
    valid = [r for r in records if not r.get("error")]
    conf = confusion(records)
    point = prf(**conf)
    amb = [1.0 if r["asked"] else 0.0 for r in valid if r["label"] == "ambiguous"]
    clr = [1.0 if r["asked"] else 0.0 for r in valid if r["label"] == "clear"]
    cis = stratified_bootstrap([int(x) for x in amb], [int(x) for x in clr])
    # single-class rates: reuse the shared helper (identical resampling scheme, seed 0)
    rc, fc = bootstrap_ci(amb, seed=0), bootstrap_ci(clr, seed=0)
    cis["recall"] = [round(rc[0], 4), round(rc[1], 4)] if rc else None
    cis["false_clarify_rate"] = [round(fc[0], 4), round(fc[1], 4)] if fc else None

    by_type: dict[str, dict[str, int]] = defaultdict(lambda: {"n": 0, "asked": 0})
    for r in valid:
        if r["label"] == "ambiguous":
            b = by_type[r.get("ambiguity_type") or "unknown"]
            b["n"] += 1
            b["asked"] += int(r["asked"])
    clear_status: dict[str, int] = defaultdict(int)
    for r in valid:
        if r["label"] == "clear":
            clear_status[r["status"]] += 1
    amb_status: dict[str, int] = defaultdict(int)
    for r in valid:
        if r["label"] == "ambiguous":
            amb_status[r["status"]] += 1
    usage = {
        k: sum(int((r.get("usage") or {}).get(k, 0)) for r in records)
        for k in ("input_tokens", "output_tokens", "llm_calls")
    }
    return {
        "n_items": len(records),
        "n_errors": len(records) - len(valid),
        "n_ambiguous": len(amb),
        "n_clear": len(clr),
        "confusion": conf,
        **{k: {"value": point[k], "ci95": cis[k]} for k in point},
        "recall_by_ambiguity_type": {
            t: {"asked": b["asked"], "n": b["n"], "recall": b["asked"] / b["n"]}
            for t, b in sorted(by_type.items())
        },
        "status_on_ambiguous": dict(sorted(amb_status.items())),
        "status_on_clear": dict(sorted(clear_status.items())),
        "false_clarify_ids": [r["id"] for r in valid if r["label"] == "clear" and r["asked"]],
        "missed_ambiguous_ids": [
            r["id"] for r in valid if r["label"] == "ambiguous" and not r["asked"]
        ],
        "usage": usage,
    }


# ── Offline Groq budget estimate (no LLM, no DB) ─────────────────────────────

DAILY_TOKENS_PER_MODEL = 200_000  # Groq free tier, per model per day
DAILY_REQUESTS_PER_MODEL = 1_000
PLANNER_TOKENS_PER_REQUEST = 5_000  # measured prompt ~3.8K (demo schema) + ~0.8K output


def budget_estimate(
    items: Sequence[dict[str, Any]],
    *,
    tokens_per_request: int = PLANNER_TOKENS_PER_REQUEST,
    expected_calls_per_item: float = 1.1,
    max_retry_attempts: int = 3,
) -> dict[str, Any]:
    """Planner-model requests/tokens/days for a full run (one planner call per item at minimum)."""
    n = len(items)
    reqs = {
        "floor": n,
        "expected": round(n * expected_calls_per_item),
        "worst": n * (1 + max_retry_attempts),
    }
    out: dict[str, Any] = {}
    for k, r in reqs.items():
        toks = r * tokens_per_request
        days = max(-(-toks // DAILY_TOKENS_PER_MODEL), -(-r // DAILY_REQUESTS_PER_MODEL))
        out[k] = {"requests": r, "tokens": toks, "days": days}
    return {
        "tokens_per_request": tokens_per_request,
        "items": n,
        "planner": out,
        "daily_caps": {
            "tokens_per_model": DAILY_TOKENS_PER_MODEL,
            "requests_per_model": DAILY_REQUESTS_PER_MODEL,
            "planner_requests_per_day_at_cap": DAILY_TOKENS_PER_MODEL // tokens_per_request,
        },
    }


# ── Reporting ────────────────────────────────────────────────────────────────


def _m(x: dict[str, Any]) -> str:
    v, ci = x["value"], x["ci95"]
    if v is None:
        return "n/a"
    return f"{100 * v:.1f}%" + (f" [{100 * ci[0]:.1f}, {100 * ci[1]:.1f}]" if ci else "")


def render_markdown(res: dict[str, Any]) -> str:
    s, m = res["summary"], res["meta"]
    c = s["confusion"]
    lines = [
        f"# Clarification eval: {res['label']}",
        "",
        f"- git: `{(m['git'].get('sha') or '')[:10]}` dirty={m['git'].get('dirty')}"
        f"  provider: `{m['provider']}`  models: {m['models']}",
        f"- dataset sha256: `{m['dataset_sha256'][:16]}`  prompts sha256: `{m['prompt_sha256'][:16]}`",
        f"- items: {s['n_items']} (ambiguous {s['n_ambiguous']}, clear {s['n_clear']}),"
        f" infrastructure errors excluded: {s['n_errors']}  complete: {res['complete']}",
        "",
        "Positive class = ambiguous. Asked = status `needs_clarification`. 95% bootstrap CIs.",
        "",
        "| metric | value [95% CI] |",
        "|---|---|",
        f"| precision | {_m(s['precision'])} |",
        f"| recall | {_m(s['recall'])} |",
        f"| F1 | {_m(s['f1'])} |",
        f"| false-clarify rate (on clear) | {_m(s['false_clarify_rate'])} |",
        "",
        f"Confusion: TP={c['tp']} FN={c['fn']} FP={c['fp']} TN={c['tn']}",
        "",
        "## Recall by ambiguity type",
        "",
        "| type | asked / n | recall |",
        "|---|---|---|",
    ]
    for t, b in s["recall_by_ambiguity_type"].items():
        lines.append(f"| {t} | {b['asked']} / {b['n']} | {100 * b['recall']:.0f}% |")
    lines += [
        "",
        f"- status on ambiguous items: {s['status_on_ambiguous']}",
        f"- status on clear items: {s['status_on_clear']}",
        f"- clear items that were (wrongly) clarified: {', '.join(s['false_clarify_ids']) or 'none'}",
        f"- ambiguous items that were not clarified: {', '.join(s['missed_ambiguous_ids']) or 'none'}",
        "",
        f"LLM calls {s['usage']['llm_calls']}, input tokens {s['usage']['input_tokens']},"
        f" output tokens {s['usage']['output_tokens']}",
        "",
    ]
    return "\n".join(lines)


def build_results(
    *,
    label: str,
    records: list[dict[str, Any]],
    n_expected: int,
    flags: dict[str, Any],
    started: datetime,
    wall_s: float,
) -> dict[str, Any]:
    from nl2sql.config import get_settings
    from nl2sql.eval.results import git_info, prompt_hash

    s = get_settings()
    return {
        "schema_version": 1,
        "suite": "clarification",
        "label": label,
        "complete": len(records) == n_expected,
        "meta": {
            "git": git_info(),
            "provider": s.llm_provider,
            "models": {"planner": s.planner_model, "fast": s.fast_model},
            "prompt_sha256": prompt_hash(),
            "dataset_sha256": dataset_sha256(),
            "db": "nl2sql_assignment",
            "flags": flags,
            "started_at": started.isoformat(timespec="seconds"),
            "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "wall_seconds": round(wall_s, 1),
        },
        "summary": summarize(records),
        "records": records,
    }


def write_results(res: dict[str, Any], out_dir: Path, label: str) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    j, m = out_dir / f"clarification_{label}.json", out_dir / f"clarification_{label}.md"
    j.write_text(json.dumps(res, indent=1, default=str) + "\n")
    m.write_text(render_markdown(res))
    return j, m


# ── Real agent wiring ────────────────────────────────────────────────────────


async def make_agent_ask() -> tuple[AskFn, Callable[[], Awaitable[None]]]:
    from nl2sql.agent.graph import build_graph, run_query
    from nl2sql.db.connection import close_pool
    from nl2sql.db.registry import DatabaseRegistry
    from nl2sql.telemetry.tracing import RequestObserver

    graph = build_graph(registry=DatabaseRegistry.from_default())

    async def ask(question: str, thread_id: str) -> dict[str, Any]:
        obs = RequestObserver()
        res = dict(await run_query(graph, question, thread_id=thread_id, callbacks=[obs]))
        res["_usage"] = {
            "input_tokens": obs.input_tokens,
            "output_tokens": obs.output_tokens,
            "llm_calls": obs.llm_calls,
        }
        return res

    return ask, close_pool


def select_items(
    items: list[dict[str, Any]], ids: str | None, limit: int | None
) -> list[dict[str, Any]]:
    if ids:
        wanted = {x.strip() for x in ids.split(",")}
        items = [i for i in items if i["id"] in wanted]
    if limit:  # up to `limit` per label so a small slice has both classes
        seen: dict[str, int] = defaultdict(int)
        kept = []
        for i in items:
            if seen[i["label"]] < limit:
                seen[i["label"]] += 1
                kept.append(i)
        items = kept
    return items


async def amain(args: argparse.Namespace) -> int:
    from nl2sql.config import get_settings

    items = select_items(load_items(), args.ids, args.limit)
    if args.budget:
        print(json.dumps(budget_estimate(items), indent=1))
        return 0
    provider = get_settings().llm_provider
    if provider != "fake" and not args.confirm_real_llm:
        print(
            f"refusing to run: LLM_PROVIDER={provider!r} would spend real model quota "
            f"(~{len(items)} agent requests). Pass --confirm-real-llm.",
            file=sys.stderr,
        )
        return 2
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240
    ckpt = out_dir / f"clarification_{args.label}.jsonl"
    done = load_checkpoint(ckpt) if args.resume else {}
    if not args.resume and ckpt.exists():
        ckpt.unlink()
    done = {k: v for k, v in done.items() if k in {i["id"] for i in items}}
    ask, closer = await make_agent_ask()
    started, t0 = datetime.now(UTC), time.perf_counter()
    print(f"{len(items)} items ({len(done)} already done)")
    try:
        records = await run_suite(
            items, ask, concurrency=args.concurrency, checkpoint=ckpt, done=done
        )
    finally:
        await closer()
    res = build_results(
        label=args.label,
        records=records,
        n_expected=len(load_items()),
        flags={
            "concurrency": args.concurrency,
            "limit_per_label": args.limit,
            "ids": args.ids,
        },
        started=started,
        wall_s=time.perf_counter() - t0,
    )
    j, m = write_results(res, out_dir, args.label)
    print(f"wrote {j} and {m}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--label", default="dev")
    ap.add_argument("--out-dir", default=str(ROOT / "results"))
    ap.add_argument("--ids", help="comma list of item ids")
    ap.add_argument("--limit", type=int, help="max items PER LABEL (small slices)")
    ap.add_argument("--concurrency", type=int, default=1)
    ap.add_argument("--no-resume", dest="resume", action="store_false")
    ap.add_argument("--budget", action="store_true", help="offline Groq budget estimate, no LLM")
    ap.add_argument("--confirm-real-llm", action="store_true")
    args = ap.parse_args()
    return asyncio.run(amain(args))


if __name__ == "__main__":
    sys.exit(main())
