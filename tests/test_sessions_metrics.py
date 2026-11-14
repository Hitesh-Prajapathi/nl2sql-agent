"""Prometheus multiprocess mode: counters from separate processes aggregate."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SNIPPET = """
from nl2sql.telemetry import metrics
metrics.REQUESTS.labels("query", "ok").inc({n})
"""
RENDER = "from nl2sql.telemetry import metrics; print(metrics.render_metrics().decode())"
SRC = str(Path(__file__).resolve().parents[1] / "src")


def _run(code: str, env_dir: Path | None) -> str:
    import os

    env = {**os.environ, "PYTHONPATH": SRC}
    env.pop("PROMETHEUS_MULTIPROC_DIR", None)
    if env_dir is not None:
        env["PROMETHEUS_MULTIPROC_DIR"] = str(env_dir)
    out = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True
    )
    return out.stdout


def test_multiprocess_aggregates_across_processes(tmp_path: Path) -> None:
    d = tmp_path / "prom"  # created by the module when missing
    _run(SNIPPET.format(n=2), d)
    _run(SNIPPET.format(n=3), d)
    text = _run(RENDER, d)
    assert 'nl2sql_requests_total{route="query",status="ok"} 5.0' in text


def test_single_process_unchanged_without_env() -> None:
    text = _run(SNIPPET.format(n=1) + "\n" + RENDER, None)
    assert 'nl2sql_requests_total{route="query",status="ok"} 1.0' in text
