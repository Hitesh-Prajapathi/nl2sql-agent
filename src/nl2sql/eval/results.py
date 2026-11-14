"""Self-describing results files and their Markdown / README renderings."""

from __future__ import annotations

import hashlib
import json
import subprocess  # noqa: S404
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nl2sql.eval.metrics import OFFICIAL_EVAL_COMMIT

README_START = "<!-- RESULTS:START -->"
README_END = "<!-- RESULTS:END -->"
FULL_DATASET_SIZE = 500


def prompt_hash(extra: list[str] | None = None) -> str:
    """Hash of every prompt template that can reach the model (plus optional extras)."""
    from nl2sql.eval.variants import NAIVE_HINT, NAIVE_SYSTEM
    from nl2sql.llm.prompts.plan_sql import CORRECTION_SYSTEM, HINT_TEMPLATE, PLAN_SQL_SYSTEM

    h = hashlib.sha256()
    for part in [PLAN_SQL_SYSTEM, CORRECTION_SYSTEM, HINT_TEMPLATE, NAIVE_SYSTEM, NAIVE_HINT, *(extra or [])]:
        h.update(part.encode())
        h.update(b"\x00")
    return h.hexdigest()


def git_info() -> dict[str, Any]:
    def run(*args: str) -> str:
        try:
            return subprocess.run(  # noqa: S603
                ["git", *args],  # noqa: S607
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""

    return {"sha": run("rev-parse", "HEAD") or None, "dirty": bool(run("status", "--porcelain"))}


def variant_label(variant: str, use_evidence: bool, limit_note: str | None) -> str:
    label = variant if use_evidence else f"{variant}-noevidence"
    return f"{label}-{limit_note}" if limit_note else label


def result_basename(label: str, when: datetime | None = None) -> str:
    when = when or datetime.now(UTC)
    return f"bird_minidev_{label}_{when:%Y-%m-%d}"


def build_results(
    *,
    label: str,
    variant: str,
    flags: dict[str, Any],
    models: dict[str, str],
    dataset_sha256: str,
    dataset_size: int,
    profile_hashes: dict[str, str],
    prompt_sha256: str,
    records: list[dict[str, Any]],
    summary: dict[str, Any],
    started_at: datetime,
    wall_s: float,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "dataset": "BIRD Mini-Dev (PostgreSQL)",
        "label": label,
        "variant": variant,
        "complete": summary.get("n_total") == dataset_size == FULL_DATASET_SIZE,
        "meta": {
            "git": git_info(),
            "models": models,
            "prompt_sha256": prompt_sha256,
            "dataset_sha256": dataset_sha256,
            "profile_sha256": profile_hashes,
            "flags": flags,
            "official_eval_commit": OFFICIAL_EVAL_COMMIT,
            "started_at": started_at.isoformat(timespec="seconds"),
            "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "wall_seconds": round(wall_s, 1),
            "cost_usd": summary.get("cost_usd"),
        },
        "summary": summary,
        "records": records,
    }


def write_results(results: dict[str, Any], out_dir: str | Path, base: str) -> tuple[Path, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    jpath, mpath = out / f"{base}.json", out / f"{base}.md"
    jpath.write_text(json.dumps(results, indent=1, default=str) + "\n")
    mpath.write_text(render_markdown(results))
    return jpath, mpath


# ── Markdown ──────────────────────────────────────────────────────────────────


def _cell(m: dict[str, Any] | None) -> str:
    if not m or m.get("value") is None:
        return "n/a"
    ci = m.get("ci95")
    return f"{m['value']:.1f} [{ci[0]:.1f}, {ci[1]:.1f}]" if ci else f"{m['value']:.1f}"


def _num(x: Any, fmt: str = "{:.2f}") -> str:
    return "n/a" if x is None else fmt.format(x)


def headline_row(results: dict[str, Any]) -> str:
    s, meta = results["summary"], results["meta"]
    rates = s.get("rates", {})
    cost = meta.get("cost_usd")
    lat = s.get("latency_s", {})
    return (
        f"| {results['label']} | {s['n_scored']}/{s['n_total']} | {_cell(s.get('ex'))} | "
        f"{_cell(s.get('soft_f1'))} | {_cell(s.get('r_ves'))} | "
        f"{_cell(rates.get('clarify'))} / {_cell(rates.get('refuse'))} / {_cell(rates.get('failed'))} | "
        f"{_num(s.get('mean_attempts'))} | "
        f"{'n/a (no price configured)' if cost is None else f'${cost:.4f}'} | "
        f"{_num(lat.get('p50'), '{:.1f}')} / {_num(lat.get('p95'), '{:.1f}')} |"
    )


HEADLINE_HEAD = (
    "| run | scored/total | EX % [95% CI] | Soft F1 % | R-VES | clarify / refuse / failed % | "
    "mean attempts | cost | latency P50 / P95 s |\n"
    "|---|---|---|---|---|---|---|---|---|"
)


def render_markdown(results: dict[str, Any]) -> str:
    s, meta = results["summary"], results["meta"]
    lines = [
        f"# BIRD Mini-Dev (PostgreSQL): {results['label']}",
        "",
        f"- git: `{(meta['git'].get('sha') or 'unknown')[:10]}`"
        + (" (dirty working tree)" if meta["git"].get("dirty") else ""),
        f"- models: {json.dumps(meta['models'])}",
        f"- flags: {json.dumps(meta['flags'])}",
        f"- dataset sha256: `{meta['dataset_sha256'][:16]}`, prompt sha256: `{meta['prompt_sha256'][:16]}`",
        f"- started: {meta['started_at']}, wall: {meta['wall_seconds']} s",
        f"- complete run: {results['complete']}"
        + ("" if results["complete"] else " (subset: NOT comparable to published numbers)"),
        "",
        HEADLINE_HEAD,
        headline_row(results),
        "",
    ]
    if s.get("n_infra_error"):
        lines += [
            f"WARNING: {s['n_infra_error']} question(s) hit LLM infrastructure errors and are "
            f"EXCLUDED from all rates: ids {s['infra_error_ids']}",
            "",
        ]
    if "by_difficulty" in s:
        lines += ["## By difficulty", "", "| difficulty | n | EX % | Soft F1 % | R-VES |", "|---|---|---|---|---|"]
        for k, v in s["by_difficulty"].items():
            lines.append(f"| {k} | {v['n']} | {v['ex']:.1f} | {v['soft_f1']:.1f} | {v['r_ves']:.1f} |")
        lines += ["", "## By database", "", "| db_id | n | EX % | Soft F1 % | R-VES |", "|---|---|---|---|---|"]
        for k, v in s["by_db_id"].items():
            lines.append(f"| {k} | {v['n']} | {v['ex']:.1f} | {v['soft_f1']:.1f} | {v['r_ves']:.1f} |")
        sl, rr = s["schema_linking"], s["retry_recovery"]
        lines += [
            "",
            "## Agent behaviour",
            "",
            f"- schema linking (table recall / precision): {_num(sl['table_recall']['mean'])} / "
            f"{_num(sl['table_precision']['mean'])}; (column recall / precision): "
            f"{_num(sl['column_recall']['mean'])} / {_num(sl['column_precision']['mean'])} "
            f"(n={sl['table_recall']['n']}, gold unparsable: {sl['gold_unparsable']})",
            f"- retry recovery: {rr['recovered']}/{rr['n_with_failed_attempt']} "
            f"({_num(rr['rate'])}) questions correct after at least one failed attempt",
            f"- tokens: {s['tokens_total']}",
            f"- cost note: {s['cost_note']}",
            f"- latency note: {s['latency_s']['note']}",
            "- R-VES: indicative only (shared database, configurable repeats; see "
            "`nl2sql.eval.metrics`)",
        ]
    return "\n".join(lines) + "\n"


# ── README tables ─────────────────────────────────────────────────────────────


def render_readme_tables(all_results: list[dict[str, Any]]) -> str:
    """Tables for COMPLETE (500-question) runs only, newest per label. Subsets are excluded."""
    latest: dict[str, dict[str, Any]] = {}
    for r in sorted(all_results, key=lambda x: x["meta"]["started_at"]):
        if r.get("complete"):
            latest[r["label"]] = r
    if not latest:
        return "_No complete BIRD Mini-Dev run has been recorded yet. Run `make eval`._\n"
    rows = [HEADLINE_HEAD] + [headline_row(r) for r in latest.values()]
    out = [
        "BIRD Mini-Dev, PostgreSQL dialect, 500 questions. Generated by `make report` from "
        "`results/bird_minidev_*.json`; do not edit by hand.",
        "",
        *rows,
        "",
        "EX by difficulty:",
        "",
        "| run | simple | moderate | challenging |",
        "|---|---|---|---|",
    ]
    for label, r in latest.items():
        bd = r["summary"].get("by_difficulty", {})
        cells = " | ".join(f"{bd[d]['ex']:.1f}" if d in bd else "n/a" for d in ("simple", "moderate", "challenging"))
        out.append(f"| {label} | {cells} |")
    return "\n".join(out) + "\n"


def update_readme(readme: Path, block: str) -> bool:
    text = readme.read_text()
    if README_START not in text or README_END not in text:
        raise ValueError(f"{readme} lacks {README_START} / {README_END} markers")
    head, rest = text.split(README_START, 1)
    _, tail = rest.split(README_END, 1)
    new = f"{head}{README_START}\n{block}{README_END}{tail}"
    if new != text:
        readme.write_text(new)
        return True
    return False
