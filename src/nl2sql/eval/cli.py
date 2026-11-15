"""``python -m nl2sql.eval.cli`` : run / profile / check-gold."""

from __future__ import annotations

import asyncio
import json
import random
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
from nl2sql.eval.promptsize import compute_prompt_sizes, size_stats
from nl2sql.eval.results import (
    build_results,
    prompt_hash,
    result_basename,
    variant_label,
    write_results,
)
from nl2sql.eval.runner import (
    SCORED_STATUSES,
    CheckpointMismatch,
    RunConfig,
    run_questions,
)
from nl2sql.eval.sqlrunner import RegistrySqlRunner, result_digest
from nl2sql.eval.summary import summarize
from nl2sql.eval.variants import SMALL_VARIANTS, VARIANTS, FullVariant, NaiveVariant, Variant
from nl2sql.llm.pacer import Pacer, set_pacer
from nl2sql.schema.capped import DEFAULT_TOTAL_CAP_TOKENS
from nl2sql.schema.ddl import build_ddl
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
    allowlist: bool = typer.Option(
        False,
        "--allowlist/--no-allowlist",
        help="validate with each database's table set, as the graph does (stored profiles)",
    ),
) -> None:
    """Run every gold SQL through the per-db roles and through validate_sql (false-block count)."""
    from nl2sql.safety.validator import validate_sql

    s = get_settings()
    data_dir = data_dir or Path(s.bird_data_dir)
    questions = load_questions(data_dir)
    dbs = sorted({q.db_id for q in questions})

    async def go() -> dict[str, Any]:
        registry = DatabaseRegistry.for_bird(dbs, use_stored_profiles=allowlist)
        runner = RegistrySqlRunner(registry)
        sem = asyncio.Semaphore(concurrency)
        table_sets: dict[str, set[str] | None] = {}
        if allowlist:
            for db in dbs:
                names = await (await registry.get(db)).schema_provider.table_names()
                table_sets[db] = set(names) if names else None

        async def one(q: Question) -> dict[str, Any]:
            v = validate_sql(q.gold_sql, allowed_tables=table_sets.get(q.db_id))
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
        "table_allowlist": allowlist,
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


def _sample_per_difficulty(questions: list[Question], per_level: int, seed: int) -> list[Question]:
    """``per_level`` questions of each difficulty, chosen with a fixed seed (stable ids)."""
    rng = random.Random(seed)  # noqa: S311 - reproducible sampling, not crypto
    picked: list[Question] = []
    for level in ("simple", "moderate", "challenging"):
        pool = sorted((q for q in questions if q.difficulty == level), key=lambda q: q.question_id)
        picked += rng.sample(pool, min(per_level, len(pool)))
    return sorted(picked, key=lambda q: q.question_id)


def _checkpoint_fingerprint(
    variant: str,
    use_evidence: bool,
    prompt_sha: str,
    dataset_sha: str,
    profile_hashes: dict[str, str],
    cap: int | None,
) -> dict[str, Any]:
    s = get_settings()
    return {
        "variant": variant,
        "evidence": use_evidence,
        "planner_model": s.planner_model,
        "fallback_model": s.fallback_model,
        "fast_model": s.fast_model,
        "reasoning_effort": s.llm_reasoning_effort,
        "planner_max_tokens": s.planner_max_tokens,
        "max_retry_attempts": s.max_retry_attempts,
        "prompt_sha256": prompt_sha,
        "dataset_sha256": dataset_sha,
        "profile_sha256": profile_hashes,
        "prompt_cap_tokens": cap,
    }


@app.command()
def run(
    variant: str = typer.Option("full", help="full | naive | full_small | naive_small"),
    limit: int = typer.Option(None, help="evaluate only the first N questions"),
    ids: str = typer.Option(None, help="comma-separated question_ids"),
    spread: bool = typer.Option(False, help="with --limit: round-robin across db_ids"),
    sample_per_difficulty: int = typer.Option(
        None, "--sample-per-difficulty", help="N questions of each difficulty (see --seed)"
    ),
    seed: int = typer.Option(42, help="seed for --sample-per-difficulty"),
    prompt_cap: int = typer.Option(
        DEFAULT_TOTAL_CAP_TOKENS,
        "--prompt-cap",
        help="*_small variants: max tokens of prompt + question + evidence + max output",
    ),
    no_cache: bool = typer.Option(False, "--no-cache", help="bypass the LLM response cache"),
    no_evidence: bool = typer.Option(False, "--no-evidence", help="do not pass BIRD evidence"),
    no_pacing: bool = typer.Option(
        False, "--no-pacing", help="disable the token-bucket / daily-budget pacer"
    ),
    reset_usage: bool = typer.Option(
        False, "--reset-usage", help="clear today's 'exhausted' flag in the usage file first"
    ),
    daily_429_limit: int = typer.Option(
        4, "--daily-429-limit", help="consecutive daily (TPD/RPD) 429s before stopping the run"
    ),
    fresh: bool = typer.Option(False, "--fresh", help="delete the checkpoint and start over"),
    concurrency: int = typer.Option(4),
    ves_repeats: int = typer.Option(3, help="timing repeats for R-VES (official uses 100)"),
    data_dir: Path = typer.Option(None),
    questions_file: Path = typer.Option(None, help="alternative questions JSON (same format)"),
    profile_dir: Path = typer.Option(None),
    cache_dir: Path = typer.Option(Path(".eval_cache/llm")),
    usage_file: Path = typer.Option(
        Path(".eval_cache/groq_usage.json"), help="per-model, per-UTC-day request counter"
    ),
    out_dir: Path = typer.Option(Path("results")),
    checkpoint: Path = typer.Option(None, help="JSONL checkpoint (default next to the results)"),
) -> None:
    """Run the evaluation, write results/bird_minidev_<variant>_<date>.{json,md}.

    The checkpoint name has no date, so rerunning the same command (for example the next day,
    after the daily request budget stopped the run) continues where it left off.
    """
    if variant not in VARIANTS:
        raise typer.BadParameter(f"variant must be one of {', '.join(VARIANTS)}")
    small = variant in SMALL_VARIANTS
    s = get_settings()
    data_dir = data_dir or Path(s.bird_data_dir)
    profile_dir = profile_dir or Path(s.profile_dir)
    all_questions = load_questions(data_dir, questions_file)
    if sample_per_difficulty is not None:
        questions = _sample_per_difficulty(all_questions, sample_per_difficulty, seed)
        note: str | None = f"n{len(questions)}-seed{seed}"
    else:
        questions = _select(all_questions, limit, ids, spread)
        note = f"n{len(questions)}" if len(questions) != len(all_questions) else None
    if not questions:
        raise typer.BadParameter("no questions selected")
    table_columns: dict[str, dict[str, list[str]]] = (
        load_table_columns(data_dir) if questions_file is None else {}
    )
    use_evidence = not no_evidence
    label = variant_label(variant, use_evidence, note)
    base = result_basename(label)
    cp = checkpoint or out_dir / f"bird_minidev_{label}.jsonl"  # no date: resumable across days
    if fresh and cp.exists():
        cp.unlink()
    started = datetime.now(UTC)
    t0 = time.perf_counter()
    cap = prompt_cap if small else None

    pacer: Pacer | None = None
    if s.llm_provider == "groq" and s.llm_pacing_enabled and not no_pacing:
        pacer = Pacer(
            tpm=s.llm_tpm_limit,
            rpd=s.llm_rpd_limit,
            state_path=usage_file,
            daily_max_strikes=daily_429_limit,
        )
        if reset_usage:
            cleared = pacer.reset_exhausted()
            typer.echo(f"--reset-usage: cleared exhausted state for {cleared or 'no models'}")
    set_pacer(pacer)  # also disables the lazy in-memory pacer when --no-pacing

    async def go() -> dict[str, Any]:
        dbs = sorted({q.db_id for q in questions})
        builder = DatabaseRegistry.for_bird(dbs, use_stored_profiles=False)
        try:
            await _ensure_profiles(builder, dbs, profile_dir)
        finally:
            await builder.close()
        registry = DatabaseRegistry.for_bird(
            dbs, profile_dir=profile_dir, prompt_cap_tokens=cap if variant == "full_small" else None
        )
        try:
            hook = EvalLLMHook(None if no_cache else DiskCache(cache_dir))
            variant_impl: Variant
            profiles = {d: load_profile(profile_path(d, profile_dir)) for d in dbs}
            profile_hashes = {d: str(p.get("content_sha256", "")) for d, p in profiles.items()}
            size_stats_ = size_stats(
                compute_prompt_sizes(
                    variant,
                    questions,
                    profiles,
                    use_evidence=use_evidence,
                    total_cap_tokens=prompt_cap,
                    max_output_tokens=s.planner_max_tokens,
                    table_budget=s.profile_table_token_budget,
                ),
                cap,
            )
            if small and size_stats_["over_cap"]:
                raise typer.BadParameter(
                    f"{size_stats_['over_cap']} questions exceed the {cap}-token cap"
                )
            if variant in ("full", "full_small"):
                variant_impl = FullVariant(build_graph(registry=registry), hook, name=variant)
            elif variant == "naive":
                ddl = {d: build_ddl(p) for d, p in profiles.items()}
                variant_impl = NaiveVariant(ddl, hook)
            else:
                variant_impl = NaiveVariant(
                    {},
                    hook,
                    name=variant,
                    profiles=profiles,
                    total_cap_tokens=prompt_cap,
                    max_output_tokens=s.planner_max_tokens,
                )
            prompt_sha = prompt_hash()
            ds_sha = dataset_hash(all_questions)
            cfg = RunConfig(
                variant=variant,
                use_evidence=use_evidence,
                concurrency=concurrency,
                checkpoint=cp,
                ves_repeats=ves_repeats,
                pacer=pacer,
                budget_models=(s.planner_model,) if pacer else (),
                calls_per_question=(1 + s.max_retry_attempts) if variant.startswith("full") else 1,
                fingerprint=_checkpoint_fingerprint(
                    variant, use_evidence, prompt_sha, ds_sha, profile_hashes, cap
                ),
            )

            def progress(rec: dict[str, Any], n: int, total: int) -> None:
                legs = ",".join(
                    f"{c['role']}:{'fb' if c['leg'] == 'fallback' else 'pl'}"
                    for c in rec.get("llm_calls") or []
                )
                typer.echo(
                    f"[{n}/{total}] q{rec['question_id']} {rec['db_id']} {rec['status']} "
                    f"ex={rec['ex']} {rec['latency_s']}s calls={legs or '-'}"
                )

            try:
                records = await run_questions(
                    questions,
                    variant_impl,
                    RegistrySqlRunner(registry),
                    table_columns,
                    cfg,
                    progress,
                )
            except CheckpointMismatch as exc:
                raise typer.BadParameter(str(exc)) from exc
            cache = hook.cache
            finished = sum(1 for r in records if r["status"] in SCORED_STATUSES)
            remaining = len(questions) - finished
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
                "prompt_cap_total_tokens": cap,
                "max_output_tokens": s.planner_max_tokens,
                "prompt_size_estimate": size_stats_,
                "pacing": (
                    {
                        "tpm": s.llm_tpm_limit,
                        "rpd": s.llm_rpd_limit,
                        "requests_today": pacer.snapshot(),
                    }
                    if pacer
                    else None
                ),
                "stopped_reason": cfg.state.stopped_reason,
                "remaining_questions": remaining,
                "sql_timeout_s": cfg.sql_timeout_s,
                "n_questions": len(questions),
                "question_ids": [q.question_id for q in questions] if note else "all",
                "sample_seed": seed if sample_per_difficulty is not None else None,
                "resumed_from_checkpoint": str(cp),
            }
            summary = summarize(records)
            return build_results(
                label=label,
                variant=variant,
                flags=flags,
                models={
                    "planner": s.planner_model,
                    "fast": s.fast_model,
                    "fallback": s.fallback_model,
                    "reasoning_effort": s.llm_reasoning_effort,
                },
                dataset_sha256=ds_sha,
                dataset_size=len(all_questions),
                profile_hashes=profile_hashes,
                prompt_sha256=prompt_sha,
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
    fl = results["meta"]["flags"]
    if fl["stopped_reason"]:
        hours = pacer.seconds_until_reset() / 3600 if pacer else 0.0
        typer.echo(
            f"STOPPED (resumable): {fl['stopped_reason']}\n"
            f"{fl['remaining_questions']} of {len(questions)} questions remain. The daily limit "
            f"resets at 00:00 UTC (in {hours:.1f} h); rerun the SAME command to continue from "
            f"{cp} (add --reset-usage to clear the stop flag sooner)."
        )
    elif fl["remaining_questions"]:
        typer.echo(
            f"{fl['remaining_questions']} questions ended in infrastructure errors; rerun the "
            "same command to retry them."
        )


if __name__ == "__main__":
    app()
