#!/usr/bin/env python3
"""
Latency benchmark runner for the NL-to-SQL agent.

Runs the 10 official latency questions (3 runs each) against the live API,
computes P50/P95 per question and overall, and saves results to
benchmarks/results/latency_<timestamp>.json + a human-readable summary.

Usage (with Docker running):
    cd /Users/hiteshprajapathi/Desktop/nl2sql-agent
    python benchmarks/run_latency.py

Or against local uvicorn:
    python benchmarks/run_latency.py --url http://127.0.0.1:8000
"""
from __future__ import annotations

import argparse
import json
import statistics
import time
import uuid
from datetime import datetime
from pathlib import Path

import urllib.request
import urllib.error

# ── Config ────────────────────────────────────────────────────────────────────
RUNS_PER_QUESTION = 3      # assignment says 3–5; 3 gives P50/P95 over 30 samples
RESULTS_DIR = Path(__file__).parent / "results"

LATENCY_QUESTIONS = [
    ("LAT-01", "How many customers are there in each tier?"),
    ("LAT-02", "What is the distribution of orders by status?"),
    ("LAT-03", "What are the 5 most expensive products by unit price?"),
    ("LAT-04", "What is the total completed payment amount broken down by payment method?"),
    ("LAT-05", "Which customers have placed more than 3 orders?"),
    ("LAT-06", "What are the top 3 product categories by total units sold on delivered orders?"),
    ("LAT-07", "For each returned order, what is the refund amount and the percentage of the order total that was refunded?"),
    ("LAT-08", "Which orders were paid less than the order total amount, and by how much?"),
    ("LAT-09", "How many open or escalated support tickets are there per support agent?"),
    ("LAT-10", "Which customers have raised a support ticket but have never placed an order?"),
]


def call_api(base_url: str, question: str) -> tuple[float, dict]:
    """POST /query and return (elapsed_seconds, response_json)."""
    thread_id = str(uuid.uuid4())
    payload = json.dumps({"question": question, "thread_id": thread_id}).encode()
    req = urllib.request.Request(
        f"{base_url}/query",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = json.loads(resp.read())
    elapsed = time.perf_counter() - t0
    return elapsed, body


def p50(values: list[float]) -> float:
    return statistics.median(values)


def p95(values: list[float]) -> float:
    if len(values) < 2:
        return values[0]
    values_sorted = sorted(values)
    idx = int(len(values_sorted) * 0.95)
    return values_sorted[min(idx, len(values_sorted) - 1)]


def run(base_url: str) -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    all_timings: list[float] = []
    results = []

    print(f"\n{'='*70}")
    print(f"  NL-to-SQL Latency Benchmark  |  {RUNS_PER_QUESTION} runs × {len(LATENCY_QUESTIONS)} questions")
    print(f"  Target: {base_url}")
    print(f"{'='*70}\n")

    for qid, question in LATENCY_QUESTIONS:
        run_times: list[float] = []
        statuses: list[str] = []
        print(f"  [{qid}] {question[:60]}...")

        for run_num in range(1, RUNS_PER_QUESTION + 1):
            try:
                elapsed, resp = call_api(base_url, question)
                run_times.append(elapsed)
                statuses.append(resp.get("status", "unknown"))
                # Extract agent timings from response
                agent_timings = resp.get("timings", {})
                node_summary = {k: f"{v}ms" for k, v in agent_timings.items()}
                print(f"    run {run_num}: {elapsed*1000:.0f}ms  status={resp.get('status')}  nodes={node_summary}")
                time.sleep(0.5)  # small pause between runs to avoid rate limiting
            except Exception as exc:
                print(f"    run {run_num}: ERROR — {exc}")
                run_times.append(float("nan"))
                statuses.append("error")

        valid_times = [t for t in run_times if not (t != t)]  # filter nan
        q_p50 = p50(valid_times) * 1000 if valid_times else float("nan")
        q_p95 = p95(valid_times) * 1000 if valid_times else float("nan")
        all_timings.extend(valid_times)

        print(f"    → P50={q_p50:.0f}ms  P95={q_p95:.0f}ms\n")

        results.append({
            "id": qid,
            "question": question,
            "runs": run_times,
            "statuses": statuses,
            "p50_ms": round(q_p50, 1),
            "p95_ms": round(q_p95, 1),
        })

    # ── Overall stats ────────────────────────────────────────────────────────
    overall_p50 = p50(all_timings) * 1000
    overall_p95 = p95(all_timings) * 1000

    print(f"{'='*70}")
    print(f"  OVERALL  P50={overall_p50:.0f}ms   P95={overall_p95:.0f}ms")
    print(f"  ({len(all_timings)} valid samples across {len(LATENCY_QUESTIONS)} questions × {RUNS_PER_QUESTION} runs)")
    print(f"{'='*70}\n")

    # ── Per-question summary table ────────────────────────────────────────────
    print(f"  {'ID':<8} {'P50 (ms)':<12} {'P95 (ms)':<12} Question")
    print(f"  {'-'*8} {'-'*12} {'-'*12} {'-'*40}")
    for r in results:
        print(f"  {r['id']:<8} {r['p50_ms']:<12.0f} {r['p95_ms']:<12.0f} {r['question'][:50]}")

    # ── Save JSON ─────────────────────────────────────────────────────────────
    output = {
        "timestamp": timestamp,
        "base_url": base_url,
        "runs_per_question": RUNS_PER_QUESTION,
        "overall_p50_ms": round(overall_p50, 1),
        "overall_p95_ms": round(overall_p95, 1),
        "questions": results,
    }
    out_path = RESULTS_DIR / f"latency_{timestamp}.json"
    out_path.write_text(json.dumps(output, indent=2))
    print(f"\n  Results saved → {out_path}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="NL-to-SQL latency benchmark")
    parser.add_argument("--url", default="http://0.0.0.0:8000", help="Base API URL")
    args = parser.parse_args()
    run(args.url)
