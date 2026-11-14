"""``python -m nl2sql.eval.cli`` : run / profile / check-gold."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer

from nl2sql.agent.graph import build_graph
from nl2sql.config import get_settings
from nl2sql.db.registry import DatabaseRegistry
from nl2sql.eval.dataset import Question, dataset_hash, load_questions, load_table_columns
from nl2sql.eval.llm_hook import DiskCache, EvalLLMHook
from nl2sql.eval.pricing import load_pricing
from nl2sql.eval.results import (
    build_results,
    prompt_hash,
    result_basename,
    variant_label,
    write_results,
)
from nl2sql.eval.runner import RunConfig, run_questions
from nl2sql.eval.sqlrunner import RegistrySqlRunner, result_digest
from nl2sql.eval.summary import summarize
from nl2sql.eval.variants import FullVariant, NaiveVariant, Variant, build_ddl
from nl2sql.schema.introspect import build_profile_data, estimate_tokens, render_context
from nl2sql.schema.profile import DatabaseProfile
from nl2sql.schema.store import load_profile, profile_path, save_profile

app = typer.Typer(help="BIRD Mini-Dev evaluation harness", no_args_is_help=True)


def _select(
    questions: list[Question], limit: int | None, ids: str | None, spread: bool
) -> list[Question]:
    if ids:
        wanted = {int(x) for x in ids.split(",") if x.strip()}
        questions = [q for q in questions if q.question_id in wanted]
    if limit is not None:
        if spread:  # round-robin over db_ids so a small sample covers several databases
            by_db: dict[str, list[Question]] = {}
            for q in questions:
                by_db.setdefault(q.db_id, []).append(q)
            picked: list[Question] = []
            while len(picked) < limit and any(by_db.values()):
                for db in sorted(by_db):
                    if by_db[db] and len(picked) < limit:
                        picked.append(by_db[db].pop(0))
            questions = picked
        else:
            questions = questions[:limit]
    return questions


async def _ensure_profiles(registry: DatabaseRegistry, db_ids: list[str], directory: Path) -> None:
    for db_id in db_ids:
        if profile_path(db_id, directory).exists():
            continue
        typer.echo(f"profile for {db_id} missing: building it now (use `make profile` to prebuild)")
        await _build_one(registry, db_id, directory)


async def _build_one(registry: DatabaseRegistry, db_id: str, directory: Path) -> Path:
    pool = await registry.pool_for(db_id)
    profile = DatabaseProfile(db_id=db_id, display_name=db_id)
    data = await build_profile_data(pool, profile)
    data["table_token_budget"] = get_settings().profile_table_token_budget
    return save_profile(data, directory)


@app.command()
def profile(
    db: list[str] = typer.Option(None, "--db", help="db_id (repeatable)"),
    all_: bool = typer.Option(False, "--all", help="every database in the dataset"),
    data_dir: Path = typer.Option(None, help="BIRD data dir (default settings.bird_data_dir)"),
    out_dir: Path = typer.Option(None, help="profile directory (default settings.profile_dir)"),
) -> None:
    """Build versioned offline profiles (pg_class.reltuples + pg_stats) as JSON."""
    s = get_settings()
    data_dir = data_dir or Path(s.bird_data_dir)
    out_dir = out_dir or Path(s.profile_dir)
    dbs = sorted({q.db_id for q in load_questions(data_dir)}) if all_ or not db else list(db)

    async def go() -> None:
        registry = DatabaseRegistry.for_bird(dbs, profile_dir=out_dir)
        try:
            for db_id in dbs:
                t0 = time.perf_counter()
                path = await _build_one(registry, db_id, out_dir)
                data = load_profile(path)
                text = render_context(data, s.profile_table_token_budget)
                per_table = [
                    estimate_tokens(text_part) for text_part in text.split("--- TABLE:")[1:]
                ]
                typer.echo(
                    f"{db_id}: {len(data['tables'])} tables, "
                    f"~{estimate_tokens(text)} prompt tokens "
                    f"(largest table ~{max(per_table, default=0)}), "
                    f"{time.perf_counter() - t0:.1f}s -> {path}"
                )
        finally:
            await registry.close()

    asyncio.run(go())


@app.command("check-gold")
def check_gold(
    data_dir: Path = typer.Option(None),
    out: Path = typer.Option(
        None, help="output JSON (default results/bird_minidev_gold_check_<date>.json)"
    ),
    concurrency: int = typer.Option(4),
) -> None:
    """Run every gold SQL through the per-db roles and through validate_sql (false-block count)."""
    from nl2sql.safety.validator import validate_sql

    s = get_settings()
    data_dir = data_dir or Path(s.bird_data_dir)
    questions = load_questions(data_dir)
    dbs = sorted({q.db_id for q in questions})

    async def go() -> dict[str, Any]:
        registry = DatabaseRegistry.for_bird(dbs, use_stored_profiles=False)
        runner = RegistrySqlRunner(registry)
        sem = asyncio.Semaphore(concurrency)

        async def one(q: Question) -> dict[str, Any]:
            v = validate_sql(q.gold_sql)
            async with sem:
                o = await runner.run(q.db_id, q.gold_sql, 30.0)
            rec: dict[str, Any] = {
                "question_id": q.question_id,
                "db_id": q.db_id,
                "difficulty": q.difficulty,
                "validator_valid": v.is_valid,
                "validator_blocked": v.is_blocked,
                "validator_error": v.error,
                "exec_error": o.error,
                "elapsed_s": round(o.elapsed_s, 4),
            }
            if o.ok and o.rows is not None:
                rec.update(result_digest(o.rows))
            return rec

        try:
            return {"records": list(await asyncio.gather(*(one(q) for q in questions)))}
        finally:
            await registry.close()

    res = asyncio.run(go())
    recs = res["records"]
    summary = {
        "n": len(recs),
        "validator_falsely_blocked": sum(1 for r in recs if not r["validator_valid"]),
        "validator_blocked_terminal": sum(1 for r in recs if r["validator_blocked"]),
        "gold_exec_errors": sum(1 for r in recs if r["exec_error"]),
        "gold_empty_results": sum(1 for r in recs if r.get("n_rows") == 0),
        "slowest_s": max((r["elapsed_s"] for r in recs), default=0),
    }
    out = out or Path("results") / f"bird_minidev_gold_check_{datetime.now(UTC):%Y-%m-%d}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"summary": summary, **res}, indent=1) + "\n")
    typer.echo(json.dumps(summary, indent=1))
    typer.echo(f"wrote {out}")


@app.command()
def run(
    variant: str = typer.Option("full", help="full | naive"),
    limit: int = typer.Option(None, help="evaluate only the first N questions"),
    ids: str = typer.Option(None, help="comma-separated question_ids"),
    spread: bool = typer.Option(False, help="with --limit: round-robin across db_ids"),
    no_cache: bool = typer.Option(False, "--no-cache", help="bypass the LLM response cache"),
    no_evidence: bool = typer.Option(False, "--no-evidence", help="do not pass BIRD evidence"),
    concurrency: int = typer.Option(4),
    ves_repeats: int = typer.Option(3, help="timing repeats for R-VES (official uses 100)"),
    data_dir: Path = typer.Option(None),
    questions_file: Path = typer.Option(None, help="alternative questions JSON (same format)"),
    profile_dir: Path = typer.Option(None),
    cache_dir: Path = typer.Option(Path(".eval_cache/llm")),
    out_dir: Path = typer.Option(Path("results")),
    checkpoint: Path = typer.Option(None, help="JSONL checkpoint (default next to the results)"),
) -> None:
    """Run the evaluation, write results/bird_minidev_<variant>_<date>.{json,md}."""
    if variant not in ("full", "naive"):
        raise typer.BadParameter("variant must be 'full' or 'naive'")
    s = get_settings()
    data_dir = data_dir or Path(s.bird_data_dir)
    profile_dir = profile_dir or Path(s.profile_dir)
    all_questions = load_questions(data_dir, questions_file)
    questions = _select(all_questions, limit, ids, spread)
    if not questions:
        raise typer.BadParameter("no questions selected")
    table_columns: dict[str, dict[str, list[str]]] = (
        load_table_columns(data_dir) if questions_file is None else {}
    )
    use_evidence = not no_evidence
    note = None
    if len(questions) != len(all_questions):
        note = f"n{len(questions)}"
    label = variant_label(variant, use_evidence, note)
    base = result_basename(label)
    cp = checkpoint or out_dir / f"{base}.jsonl"
    started = datetime.now(UTC)
    t0 = time.perf_counter()

    async def go() -> dict[str, Any]:
        dbs = sorted({q.db_id for q in questions})
        builder = DatabaseRegistry.for_bird(dbs, use_stored_profiles=False)
        try:
            await _ensure_profiles(builder, dbs, profile_dir)
        finally:
            await builder.close()
        registry = DatabaseRegistry.for_bird(dbs, profile_dir=profile_dir)
        try:
            hook = EvalLLMHook(None if no_cache else DiskCache(cache_dir))
            variant_impl: Variant
            profile_hashes = {
                d: str(load_profile(profile_path(d, profile_dir)).get("content_sha256", ""))
                for d in dbs
            }
            if variant == "full":
                variant_impl = FullVariant(build_graph(registry=registry), hook)
            else:
                ddl = {d: build_ddl(load_profile(profile_path(d, profile_dir))) for d in dbs}
                variant_impl = NaiveVariant(ddl, hook)
            cfg = RunConfig(
                variant=variant,
                use_evidence=use_evidence,
                concurrency=concurrency,
                checkpoint=cp,
                ves_repeats=ves_repeats,
            )

            def progress(rec: dict[str, Any], n: int, total: int) -> None:
                typer.echo(
                    f"[{n}/{total}] q{rec['question_id']} {rec['db_id']} {rec['status']} "
                    f"ex={rec['ex']} {rec['latency_s']}s"
                )

            records = await run_questions(
                questions, variant_impl, RegistrySqlRunner(registry), table_columns, cfg, progress
            )
            cache = hook.cache
            flags = {
                "variant": variant,
                "evidence": use_evidence,
                "cache": cache is not None,
                "cache_hits": cache.hits if cache else 0,
                "cache_misses": cache.misses if cache else 0,
                "concurrency": concurrency,
                "ves_repeats": ves_repeats,
                "max_retry_attempts": s.max_retry_attempts,
                "llm_provider": s.llm_provider,
                "profile_table_token_budget": s.profile_table_token_budget,
                "sql_timeout_s": cfg.sql_timeout_s,
                "n_questions": len(questions),
                "question_ids": [q.question_id for q in questions] if note else "all",
                "resumed_from_checkpoint": str(cp),
            }
            summary = summarize(records, load_pricing())
            return build_results(
                label=label,
                variant=variant,
                flags=flags,
                models={
                    "planner": s.planner_model,
                    "fast": s.fast_model,
                    "fallback": s.fallback_model,
                },
                dataset_sha256=dataset_hash(all_questions),
                dataset_size=len(all_questions),
                profile_hashes=profile_hashes,
                prompt_sha256=prompt_hash(),
                records=records,
                summary=summary,
                started_at=started,
                wall_s=time.perf_counter() - t0,
            )
        finally:
            await registry.close()

    results = asyncio.run(go())
    jpath, mpath = write_results(results, out_dir, base)
    sm = results["summary"]
    typer.echo(
        json.dumps({k: sm.get(k) for k in ("n_total", "n_scored", "n_infra_error", "ex")}, indent=1)
    )
    typer.echo(f"wrote {jpath} and {mpath}")


if __name__ == "__main__":
    app()
