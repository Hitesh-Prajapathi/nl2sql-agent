"""Load-test runner (mock LLM): starts uvicorn, drives headless Locust per level.

    uv run --extra loadtest python loadtest/run.py --levels 1,10,50,100

Writes results/loadtest_mock_<date>[_<tag>].json and .md. Percentiles/RPS/error rate
use only the steady-state window (after ramp). All LLM latency is SIMULATED (see
loadtest/fake_script.py); results describe this system's capacity, not a provider's.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any

import psutil

ROOT = Path(__file__).resolve().parent.parent
PORT = int(os.environ.get("LT_PORT", "8765"))
BASE = f"http://127.0.0.1:{PORT}"


def http(method: str, path: str, timeout: float = 5.0) -> Any:
    req = urllib.request.Request(BASE + path, method=method, data=b"" if method == "POST" else None)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def pct(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    return s[min(len(s) - 1, int(len(s) * q))]


def git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True
        ).strip()
    except Exception:
        return "unknown"


def start_server(env: dict[str, str], workers: int) -> subprocess.Popen[bytes]:
    cmd = [
        sys.executable, "-m", "uvicorn", "loadtest.server:app",
        "--port", str(PORT), "--workers", str(workers), "--log-level", "warning",
    ]  # fmt: skip
    proc = subprocess.Popen(
        cmd, cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    for _ in range(60):
        time.sleep(0.5)
        if proc.poll() is not None:
            raise RuntimeError("uvicorn exited during startup")
        try:
            if http("GET", "/readyz").get("status") == "ready":
                return proc
        except Exception:
            continue
    proc.kill()
    raise RuntimeError("server not ready in 30s")


def verify_followups(n: int = 40) -> dict[str, Any]:
    """Two-turn conversations over fresh connections (so they spread across workers); then
    check the shared Postgres store holds both turns for each thread."""
    import asyncio
    import uuid

    import asyncpg

    first, second = "How many customers are there?", "And how many of them are active?"
    tids = [str(uuid.uuid4()) for _ in range(n)]
    ok = 0
    for tid in tids:
        sts = []
        for q in (first, second):
            req = urllib.request.Request(
                BASE + "/v1/query",
                data=json.dumps({"question": q, "thread_id": tid}).encode(),
                headers={"Content-Type": "application/json", "Connection": "close"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=60) as r:
                sts.append(json.loads(r.read()).get("status"))
        ok += all(s in ("answered", "success", "needs_clarification", "clarification") for s in sts)

    async def counts() -> list[int]:
        url = os.environ.get(
            "SESSION_DATABASE_URL",
            "postgresql://app_state_writer:app_state_pass@localhost:5432/nl2sql_assignment",
        )
        conn = await asyncpg.connect(url)
        try:
            rows = await conn.fetch(
                "SELECT jsonb_array_length(turns) AS n FROM app_state.sessions "
                "WHERE session_id = ANY($1::text[])",
                tids,
            )
            return [r["n"] for r in rows]
        finally:
            await conn.close()

    lens = asyncio.run(counts())
    return {
        "threads": n,
        "http_ok_pairs": ok,
        "threads_in_store": len(lens),
        "threads_with_two_turns": sum(1 for x in lens if x == 2),
    }


def stop(proc: subprocess.Popen[bytes]) -> None:
    try:
        parent = psutil.Process(proc.pid)
        kids = parent.children(recursive=True)
    except psutil.Error:
        kids = []
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
    for k in kids:
        try:
            k.kill()
        except psutil.Error:
            pass


def sampler(
    server: psutil.Process,
    locust_pid: int,
    t_start: float,
    ramp: float,
    steady: float,
    out: dict[str, Any],
) -> None:
    """Every 2 s during steady state: pool size/idle, server CPU%/RSS, locust CPU%."""
    try:
        loc = psutil.Process(locust_pid)
    except psutil.Error:
        return
    procs = [server, *server.children(recursive=True)]
    for p in procs:
        p.cpu_percent(None)
    loc.cpu_percent(None)
    time.sleep(max(0.0, t_start + ramp - time.time()))
    try:
        http("POST", "/_loadtest/reset")
    except Exception:
        pass
    samples: list[dict[str, Any]] = []
    end = t_start + ramp + steady
    while time.time() < end - 1:
        time.sleep(2)
        try:
            st = http("GET", "/_loadtest/stats")
            samples.append(
                {
                    "pool": st["pool"],
                    "server_cpu_pct": sum(p.cpu_percent(None) for p in procs),
                    "server_rss_mb": sum(p.memory_info().rss for p in procs) / 1e6,
                    "locust_cpu_pct": loc.cpu_percent(None),
                }
            )
            out["last_stats"] = st
        except Exception:
            continue
    out["samples"] = samples


def run_level(
    users: int, ramp: int, steady: int, server: psutil.Process, raw: Path
) -> dict[str, Any]:
    env = {**os.environ, "LT_RAW": str(raw), "PYTHONPATH": f"{ROOT}/src:{ROOT}"}
    rate = max(1.0, users / ramp)
    cmd = [
        sys.executable, "-m", "locust", "-f", str(ROOT / "loadtest/locustfile.py"),
        "--headless", "--host", BASE, "-u", str(users), "-r", str(rate),
        "-t", f"{ramp + steady}s", "--only-summary", "--loglevel", "ERROR",
    ]  # fmt: skip
    if raw.exists():
        raw.unlink()
    t0 = time.time()
    proc = subprocess.Popen(
        cmd, cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    while not raw.exists() and time.time() - t0 < 30:
        time.sleep(0.2)
    meta = json.loads(raw.read_text().splitlines()[0])["meta"]
    t_start = float(meta["test_start"])
    side: dict[str, Any] = {}
    th = threading.Thread(target=sampler, args=(server, proc.pid, t_start, ramp, steady, side))
    th.start()
    try:
        proc.wait(timeout=ramp + steady + 60)
    except subprocess.TimeoutExpired:
        proc.kill()
    th.join()

    lo, hi = t_start + ramp, t_start + ramp + steady
    recs = [json.loads(line) for line in raw.read_text().splitlines()[1:]]
    win = [r for r in recs if lo <= r["t"] < hi]
    ms = [r["ms"] for r in win]
    fails = sum(1 for r in win if not r["ok"])
    by_name: dict[str, list[float]] = {}
    for r in win:
        by_name.setdefault(r["name"], []).append(r["ms"])
    samples = side.get("samples", [])
    last = side.get("last_stats", {})
    return {
        "users": users,
        "requests": len(win),
        "rps": round(len(win) / steady, 2),
        "error_rate_pct": round(100 * fails / len(win), 2) if win else None,
        "p50_ms": round(pct(ms, 0.50)),
        "p95_ms": round(pct(ms, 0.95)),
        "p99_ms": round(pct(ms, 0.99)),
        "per_name_p50_ms": {k: round(pct(v, 0.5)) for k, v in sorted(by_name.items())},
        "pool_size_max": max((s["pool"]["size"] for s in samples if s["pool"]), default=None),
        "pool_idle_min": min((s["pool"]["idle"] for s in samples if s["pool"]), default=None),
        "pool_max": last.get("pool", {}).get("max") if last.get("pool") else None,
        "acquire_wait_ms": last.get("acquire_wait_ms"),
        "loop_lag_ms": last.get("loop_lag_ms"),
        # the lag monitor ticks every ~52 ms; far fewer ticks than expected means the
        # host was suspended or throttled (e.g. macOS sleep), so the level is invalid
        "valid": bool(last.get("loop_lag_ms", {}).get("n", 0) >= 0.8 * steady / 0.052),
        "server_cpu_pct_mean": round(sum(s["server_cpu_pct"] for s in samples) / len(samples), 1)
        if samples
        else None,
        "server_cpu_pct_max": round(max((s["server_cpu_pct"] for s in samples), default=0), 1),
        "server_rss_mb_max": round(max((s["server_rss_mb"] for s in samples), default=0), 1),
        "locust_cpu_pct_mean": round(sum(s["locust_cpu_pct"] for s in samples) / len(samples), 1)
        if samples
        else None,
    }


def md_table(rows: list[dict[str, Any]]) -> str:
    out = [
        "| Users | Valid | RPS | Err % | P50 ms | P95 ms | P99 ms | Pool size/idle-min/max | Acquire wait P95 ms | Loop lag P95 ms | Server CPU % (mean/max) | RSS MB | Locust CPU % |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        aw, ll = r["acquire_wait_ms"] or {}, r["loop_lag_ms"] or {}
        out.append(
            f"| {r['users']} | {'yes' if r['valid'] else 'NO (host stalled)'} | {r['rps']} | {r['error_rate_pct']} | {r['p50_ms']} | {r['p95_ms']} | {r['p99_ms']} "
            f"| {r['pool_size_max']}/{r['pool_idle_min']}/{r['pool_max']} | {aw.get('p95', 0):.1f} | {ll.get('p95', 0):.1f} "
            f"| {r['server_cpu_pct_mean']}/{r['server_cpu_pct_max']} | {r['server_rss_mb_max']} | {r['locust_cpu_pct_mean']} |"
        )
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--levels", default="1,10,50,100")
    ap.add_argument("--ramp", type=int, default=15)
    ap.add_argument("--steady", type=int, default=60)
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--tag", default="")
    ap.add_argument("--pool-max", type=int, default=0, help="override asyncpg pool max_size")
    ap.add_argument("--llm-concurrency", type=int, default=0, help="override LLM_MAX_CONCURRENCY")
    ap.add_argument("--session-backend", default="", help="SESSION_BACKEND (memory|postgres)")
    ap.add_argument("--prom-dir", default="", help="PROMETHEUS_MULTIPROC_DIR")
    ap.add_argument("--verify-followups", action="store_true")
    ap.add_argument("--baseline", default="", help="baseline results JSON to compare against")
    ap.add_argument("--notes", default="")
    args = ap.parse_args()

    env = {
        **os.environ,
        "LLM_PROVIDER": "fake",
        "RATE_LIMIT_ENABLED": "false",  # per-IP limiter would throttle the single load-gen IP
        "AUDIT_ENABLED": "false",  # do not write to the shared database
        "PYTHONPATH": f"{ROOT}/src:{ROOT}",
        "LT_LATENCY_SEED": "1234",
    }
    if args.pool_max:
        env["LT_POOL_MAX"] = str(args.pool_max)
    if args.llm_concurrency:
        env["LLM_MAX_CONCURRENCY"] = str(args.llm_concurrency)

    from loadtest.fake_script import LatencyParams

    if args.session_backend:
        env["SESSION_BACKEND"] = args.session_backend
    if args.prom_dir:
        env["PROMETHEUS_MULTIPROC_DIR"] = args.prom_dir

    lat = LatencyParams.from_env()
    srv = start_server(env, args.workers)
    results: list[dict[str, Any]] = []
    try:
        server_proc = psutil.Process(srv.pid)
        raw = Path(os.environ.get("TMPDIR", "/tmp")) / "lt_raw.jsonl"
        for u in [int(x) for x in args.levels.split(",")]:
            print(f"== {u} users: ramp {args.ramp}s + steady {args.steady}s", flush=True)
            r = run_level(u, args.ramp, args.steady, server_proc, raw)
            print(
                json.dumps(
                    {
                        k: r[k]
                        for k in (
                            "users",
                            "rps",
                            "error_rate_pct",
                            "p50_ms",
                            "p95_ms",
                            "p99_ms",
                            "valid",
                        )
                    }
                ),
                flush=True,
            )
            results.append(r)
        followup_check = verify_followups() if args.verify_followups else None
    finally:
        stop(srv)

    today = date.today().isoformat()
    stem = f"loadtest_mock_{today}" + (f"_{args.tag}" if args.tag else "")
    meta = {
        "mode": "mock-LLM (LLM_PROVIDER=fake); latencies are ASSUMPTIONS, not measured",
        "date": today,
        "git_sha": git_sha(),
        "tag": args.tag,
        "notes": args.notes,
        "workers": args.workers,
        "session_backend": args.session_backend or "memory",
        "followup_check": followup_check,
        "ramp_s": args.ramp,
        "steady_s": args.steady,
        "settings": {
            "pool_min_size": 2,
            "pool_max_size": args.pool_max or 10,
            "llm_max_concurrency": args.llm_concurrency or 32,
            "rate_limit_enabled": False,
            "audit_enabled": False,
            "traffic_mix": "70% simple / 20% complex joins / 10% 2-turn follow-ups (same thread_id)",
            "think_time_s": "uniform 0.2-1.0",
            "endpoint": "POST /v1/query",
        },
        "fake_latency_ms_lognormal": {
            "plan_median": lat.plan_median_ms,
            "plan_p95": lat.plan_p95_ms,
            "fast_median": lat.fast_median_ms,
            "fast_p95": lat.fast_p95_ms,
        },  # fmt: skip
    }
    (ROOT / "results" / f"{stem}.json").write_text(
        json.dumps({**meta, "levels": results}, indent=2)
    )
    md = [
        f"# Load test (mock LLM) {today}" + (f" [{args.tag}]" if args.tag else ""),
        "",
        f"Git SHA `{meta['git_sha']}`, {args.workers} uvicorn worker(s), {args.ramp}s ramp + {args.steady}s steady state per level.",
        "",
        "**Mock-LLM mode.** The fake LLM returns canned, valid SQL and sleeps for a sampled lognormal latency, so requests run the real validate, execute and synthesize path against Postgres. "
        f"Latencies are ASSUMPTIONS until replaced with samples from real Langfuse traces: planner median {lat.plan_median_ms:.0f} ms / P95 {lat.plan_p95_ms:.0f} ms, "
        f"fast (synthesis) median {lat.fast_median_ms:.0f} ms / P95 {lat.fast_p95_ms:.0f} ms. "
        "Result sets in the mix are small, so synthesis is templated and the fast-LLM latency is not exercised.",
        "",
        f"Settings: `{json.dumps(meta['settings'])}`",
        "",
        args.notes,
        "",
        md_table(results),
    ]
    if args.baseline:
        base = json.loads(Path(args.baseline).read_text())
        bmap = {r["users"]: r for r in base["levels"]}
        md += ["", f"## Before ({base.get('tag') or 'baseline'}, `{json.dumps(base['settings'])}`) vs after", "",
               "| Users | RPS before | RPS after | P50 before | P50 after | P95 before | P95 after | P99 before | P99 after | Err % before | Err % after |",
               "|---|---|---|---|---|---|---|---|---|---|---|"]  # fmt: skip
        for r in results:
            b = bmap.get(r["users"])
            if b:
                md.append(
                    f"| {r['users']} | {b['rps']} | {r['rps']} | {b['p50_ms']} | {r['p50_ms']} | {b['p95_ms']} | {r['p95_ms']} "
                    f"| {b['p99_ms']} | {r['p99_ms']} | {b['error_rate_pct']} | {r['error_rate_pct']} |"
                )
    (ROOT / "results" / f"{stem}.md").write_text("\n".join(md) + "\n")
    print(f"wrote results/{stem}.json and .md")


if __name__ == "__main__":
    main()
