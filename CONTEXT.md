# Session handoff: NL2SQL agent refinement

> Written 2026-11-14 23:10 IST to hand work over to a new chat. Read this first, then
> `docs/REFINEMENT_EXECUTION_PLAN.md` (the master plan) for detail.

## 1. Goal

Harden this repo into an interview-proof portfolio project. Every claim must be
reproducible with one command, measured on public data (BIRD Mini-Dev, PostgreSQL),
compared against a baseline, and backed by a file in `results/`. The original brief
is `01_nl2sql_agent.md`. The full plan, including the audit of bugs and claim gaps, is
`docs/REFINEMENT_EXECUTION_PLAN.md`.

## 2. How the user wants work done

- **The main session plans, reviews and merges. Subagents do the implementation**, on
  cheaper models: Sonnet at **high effort at most** for hard or security work, Sonnet
  medium for routine work, Haiku for docs. Never run subagents on Opus. Don't use
  `fork` agents, because they inherit the main model.
- **Run 2 subagents at a time, not 4.** Four in parallel hit the Sonnet 5-hour usage
  limit three times.
- Each subagent runs with `isolation: worktree` and owns a disjoint set of files.
- **Model prices and cost must not appear anywhere.** The user asked for them to be
  removed, and they have been.
- Ask before spending Groq quota on anything beyond the planned headline runs.
- Never push, and never touch `master`. All work stays on the local `refinement` branch.

## 3. Repository state

- Branch `refinement` at commit `2bcb3f0`. **Nothing is pushed. `master` is untouched.**
- Tag `v1.0-baseline` marks Phase 0 plus Phase 5, with the ORIGINAL validator (before
  the red-team hardening).
- Checks on `refinement`: **1433 passed, 13 skipped** (the skips are Redis tests).
  `ruff check .`, `ruff format --check .` and `mypy src` (strict) are all clean.
- Commit attribution line: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`
  (main session). Subagents use their own model name.

### Done and merged

| Phase | What | Key result / artifact |
|---|---|---|
| 0 | Bugs B1–B7 fixed (blocked or failed queries reported as answered, blind self-correction, budget check, full-result fetch, domain hardcoding), fake LLM provider (`LLM_PROVIDER=fake`), routing and end-to-end tests, packaging (multi-stage non-root Dockerfile, compose dev and prod profiles), ruff and mypy strict | tests |
| 1 | BIRD Mini-Dev harness `src/nl2sql/eval/`: `make bird-setup`, `make eval VARIANT=full\|naive\|full_small\|naive_small`; multi-DB `DatabaseRegistry`; profiling from pg_stats; metrics: execution accuracy (EX), Soft-F1, R-VES, schema-linking recall, 95% bootstrap CI; resumable JSONL checkpoints; pacer that respects Groq limits | `docs/BIRD_SETUP.md`; 0/500 gold SQL falsely blocked: `results/bird_minidev_gold_check_2026-11-14.json` |
| 4 | SQL red-team suite `redteam/` (436 attacks, 75 benign queries, 100 NL-guard items), allowlist validator | static block rate 62.2% → **100%**; benign false-block 0%; `results/redteam_sql_v1/v2.json`. NL-guard TPR 1.00 / FPR 0.00 is **overfit**; on fresh items it scored 13/15 TPR and 0/15 FPR. |
| 4.5 | DB-layer hardening `db/hardening.sql` (REVOKE on dangerous functions and catalog views, pinned search_path), applied to the live DB | attacks that would succeed against the DB: 170 → **54**; single-defender payloads 218 → 94; `results/redteam_sql_v3.json` |
| 5 | API `/v1/query` (auth via hashed keys, rate limits, typed errors, request IDs, `/healthz` and `/readyz`), SSE streaming, LLM resilience (timeouts, backoff, fallback, circuit breaker, semaphore), audit log `audit.query_log` written by the `audit_writer` role, OTel and Prometheus `/metrics` plus a Grafana dashboard, session store (memory, postgres or redis) with Prometheus multiprocess support, API `database` field | `db/audit.sql`, `db/app_state.sql`, `ops/grafana/` |
| 6 | Mock-LLM load test `loadtest/` | knee at about 10 users, caused by the `LLM_MAX_CONCURRENCY=8` semaphore (default now 32). At concurrency 64: 55.8 RPS and P95 2.6 s at 100 users. `results/loadtest_mock_2026-11-14_*.md` |
| 7 | GitHub Actions workflows `.github/workflows/` (ci, eval-smoke, eval-nightly, docker) and dependabot | eval commands are still placeholders (a subagent is filling them in, see section 5) |

## 4. BIRD benchmark: in progress

- **Models** (in `.env`, which is set): `PLANNER_MODEL=openai/gpt-oss-120b`,
  `FAST_MODEL=openai/gpt-oss-20b`, `FALLBACK_MODEL=openai/gpt-oss-20b`,
  `LLM_MAX_CONCURRENCY=32`. Llama 3.3 70B is no longer on Groq, so the old docs that
  mention it are stale.
- **Groq free-tier limits** for every model: 1000 requests/day and 8000 tokens/min.
  **The real binding limit is 200,000 tokens/day per model (TPD)**, which is about 70
  questions/day. The TPD window rolls. The pacer (`src/nl2sql/llm/pacer.py`) logs every
  429 and backs off on a TPD 429 (it honours retry-after, else waits 5/10/20/30 min). It
  stops resumable only after repeated daily strikes. State is kept in
  `.eval_cache/groq_usage.json`; pass `--reset-usage` to clear it.
- **Variant `full_small`**: caps the schema context so every request stays under
  ~6.5K tokens (max 6,490, mean 5,766 over all 500 questions). `full` (max 9,060)
  cannot run on this tier. Report it as "smaller-schema variant".
- **User decision: headline runs use a fixed 150-question stratified subset**,
  `config/bird_subset_150.txt` (seed 20261114; simple/moderate/challenging = 44/75/31;
  generated by `scripts/make_subset.py`). It is NOT all 500.
- **Status:** `full_small` on the subset has **26/150 done (13 correct, interim and not
  meaningful)**. It was running in the background from the old chat and was waiting on a
  TPD 429. **It will probably die when the old chat closes. Restart it (it resumes from
  the checkpoint):**

  ```bash
  cd /Users/hiteshprajapathi/Desktop/nl2sql-agent
  docker start nl2sql_postgres   # the container sometimes exits when the laptop sleeps
  IDS=$(grep -v '^#' config/bird_subset_150.txt | paste -sd, -)
  caffeinate -i make eval VARIANT=full_small IDS="$IDS" \
    EVAL_ARGS="--no-cache --checkpoint results/bird_minidev_full_small.jsonl" \
    > results/logs/full_small_subset150_$(date +%Y%m%d_%H%M).log 2>&1
  ```

  Check the process is not already running first:
  `ps aux | grep "nl2sql.eval.cli run" | grep -v grep`.
- **Then** run `naive_small` on the same subset (same command with `VARIANT=naive_small`
  and checkpoint `results/bird_minidev_naive_small.jsonl`). That is about 2 more days.
- An aborted run over all 500 questions produced 69 questions (37 correct) in the same
  checkpoint file. Only the 26 that fall in the subset count toward the headline.
- BIRD data lives in the **gitignored** `data/` folder of the main checkout (1.7 GB),
  plus a Postgres database `bird_minidev` with one schema and one read-only role
  `bird_ro_<db_id>` per BIRD database.

## 5. Update 2026-11-15 (second chat)

- The old `full_small` process hung for 9 h after the laptop slept, mid-backoff. It was killed
  and restarted at 08:34 IST (log `results/logs/full_small_subset150_20261115_0834.log`). If a
  log goes quiet for more than about 35 min with no 429 line, suspect the same hang.
- Both subagents below were re-run and merged into `refinement`:
  - 1 vs 4 workers: `results/loadtest_mock_2026-11-15_workers.*`. At 100 users, 27.7 → 58.9
    RPS and P95 4.2 s → 2.6 s; follow-ups 40/40 across workers. The gain comes from the
    aggregate LLM semaphore (4 × 32), not CPU. The CI smoke and nightly eval commands are
    real now; the smoke baseline stays 0 until the first green run.
  - Clarification set (100 items, `make eval-clarify`) and NL red team (228 attacks + 64
    benign, `make redteam-nl`, DB `nl2sql_redteam` via `make redteam-nl-setup`). Real runs
    need `--confirm-real-llm` and the user's approval. Estimated quota: red team about 12
    days, clarification about 3 days.
  - Finding: the profiler SELECTs every table in the profiled schema, so an unreadable table
    in `public` breaks the agent.
- Tests: 1492 passed, 13 skipped.

## 5a. (Historical) Subagents launched just before the first handoff

Both were started from the old chat at about 23:08 IST and work in
`.claude/worktrees/agent-*` on `worktree-agent-*` branches. If the old chat closed
before they finished, find their state with:
`git branch --list 'worktree-agent-*' --no-merged refinement`, then look at each
worktree's `git log refinement..HEAD` and `git status`. Commit or finish the work, then
merge into `refinement`.

1. **4-worker load test and CI fill-ins** (Sonnet medium):
   - Apply `db/app_state.sql`.
   - Compare 1 vs 4 workers with `SESSION_BACKEND=postgres` and the fake LLM, and
     verify follow-up questions work across workers.
   - Write `results/loadtest_mock_<date>_workers.*`.
   - Replace the CI placeholders with real commands; fill `.github/eval_smoke_ids.txt`
     with 25 IDs from the subset; make the nightly job run the 150-question subset.
2. **Clarification set and NL red-team suite, built offline only** (Sonnet medium):
   - `evalsets/clarification/`: 50 ambiguous + 50 clear questions, runner
     `make eval-clarify`.
   - `redteam/nl/`: 200+ prompts, including indirect injection seeded into a SEPARATE
     database `nl2sql_redteam`, plus 60+ benign hard negatives. Objective oracles:
     marker strings, a canary schema, a write checksum, catalog strings, wall time.
     Runner `make redteam-nl`.
   - Includes an estimate of the Groq budget each suite needs.
   - Neither suite has been run against the real model yet.

## 6. What's left (in order)

1. Finish the `full_small` and then `naive_small` headline runs on the subset. Then
   `make report` to fill the README tables between the `<!-- RESULTS:START/END -->`
   markers. Report EX per difficulty with CIs, B0 (naive) vs the agent, a cited
   leaderboard reference for PostgreSQL, and the limitations (subset, smaller schema,
   free tier, contamination).
2. Phase 1.8: label about 100 failures into categories (the subset gives fewer; label
   all of them).
3. Phase 2 ablations on the subset, each ~150 questions, about 2 days of quota: no
   profiler, no self-correction (`MAX_RETRY_ATTEMPTS=0`), no CoT, with vs without
   evidence, `gpt-oss-20b` vs 120b. Use paired McNemar tests.
4. Phase 3 accuracy work, chosen from the failure categories (value retrieval with
   pg_trgm, few-shot examples from BIRD train, multi-candidate voting, dialect lint).
5. Run the clarification and NL red-team suites against the real model (quota
   permitting). Implement Layer 6 data delimiters (spotlighting) in the synthesis and
   planner prompts, and measure before/after with the indirect-injection category.
6. Phase 8: README rewrite with generated tables, a Limitations section, and
   regenerated WRITEUP / INTERVIEW_PREP / CODEBASE_WALKTHROUGH (they contain stale
   claims: Llama 70B, the "7 layers" wording, old numbers). Hosted demo, video, CI
   badge. Push only when the user asks.

## 7. Gotchas

- **Worktrees branch from `master`, not `refinement`.** Every subagent prompt must start
  with `git merge --ff-only refinement`.
- **In a worktree, `uv run` imports the MAIN checkout's `src`** (editable install).
  Always run `PYTHONPATH=$PWD/src uv run ...`.
- `data/` and `.env` are gitignored, so worktrees don't have them. Copy them in when a
  task needs them.
- **The shared Postgres container `nl2sql_postgres` is used by everything.** Never run
  `docker compose down -v`, which would wipe the BIRD data and the hardening.
- The DB hardening revoked `pg_sleep` and others from PUBLIC. Tests that need delays use
  heavy `generate_series` queries. asyncpg needs `pg_advisory_unlock_all` (granted back).
- `uv.lock` changes on every merge that adds dependencies. Relock and commit it on
  `refinement` after merging (`uv lock && uv sync --all-extras`).
- Cloudflare blocks Python's default urllib user agent on api.groq.com (error 1010).
  Use curl or the SDK for probes.
- Interim accuracy numbers are not results. Only quote numbers from a finished results
  file, with their CI.

## 8. Useful commands

```bash
uv run pytest -q                      # full suite (needs Postgres up)
uv run ruff check . && uv run ruff format --check . && uv run mypy src
make redteam-sql                      # SQL red-team suite
make loadtest                         # mock-LLM load test
make eval VARIANT=full_small IDS=...  # BIRD (see section 4)
make report                           # regenerate README results tables
cat .eval_cache/groq_usage.json       # pacer state / last 429 message
```

Memory notes for Claude live in
`~/.claude/projects/-Users-hiteshprajapathi-Desktop-nl2sql-agent/memory/`
(`nl2sql-refinement-plan.md`, `subagent-model-tiering.md`).
