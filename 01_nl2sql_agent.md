# Refinement Plan — NL2SQL Agent

> **Context:** This repo is one of several portfolio projects I'm hardening for AI/ML Engineer job applications. The goal is to be interview-proof: every claim has to be reproducible, benchmarked on public data, and compared against baselines. This file is the working plan for this repo.

## Current state
- LangGraph 6-node state machine (load_schema → plan_sql → route → validate → execute → correct → synthesize)
- Groq Llama 3.3 70B (generation) + Llama 3.1 8B (synthesis), PostgreSQL 16 via asyncpg
- 7-layer safety: read-only role, `BEGIN READ ONLY` + timeouts, SQLGlot AST validation, injection regex, LIMIT injection, data delimiters, audit log
- Self-correction loop (max 3), schema profiler with cache (69 ms → 3 ms), Langfuse/OTel/structlog, Docker Compose
- Evaluation: **20 self-written golden cases.** This is the main weakness.

## Gaps an interviewer will hit
- "What's your accuracy on a standard benchmark?" (no answer yet)
- "How do you know the safety layers work?" (no attack suite or measured block rate)
- "What does each component contribute?" (no ablations)
- "How does it behave under concurrent load?" (single-user latency only)

## Plan

### Phase 1: Public benchmark accuracy (highest priority)
- Integrate **BIRD Mini-Dev (500 questions, PostgreSQL version)**. It matches the existing Postgres stack.
- Metrics: **Execution Accuracy (EX)** overall and by difficulty (simple/moderate/challenging), plus **R-VES/VES** (efficiency).
- Optional stretch: Spider 2.0-Lite subset for enterprise-scale schemas.
- Report the cost and latency of the benchmark run alongside accuracy.

### Phase 2: Ablations
Run BIRD Mini-Dev with each component toggled:
| Variant | Measures |
|---|---|
| No schema profiler (raw DDL) | EX, prompt tokens |
| No self-correction | EX, retry-recovery rate |
| No CoT planning | EX |
| 8B vs 70B for generation | EX vs cost |
| Different provider/model (e.g. one open, one frontier) | EX vs cost vs latency |

### Phase 3: Safety red-team suite
- Build a labeled attack set of 150–300 prompts: direct and indirect prompt injection, DML/DDL smuggling, multi-statement, `pg_sleep`/`dblink`/`pg_read_file`, catalog probing, jailbreaks, and data-exfiltration attempts. Use **promptfoo** or **garak** for generation and running.
- Pair it with a **benign set** (BIRD questions) to measure over-blocking.
- Metrics: **Attack Success Rate (ASR)** overall and per layer, **False Refusal Rate**, and which layer caught each attack. The "defense in depth" story becomes a table.

### Phase 4: Production behavior
- Load test with **k6 or Locust** at 1/10/50 concurrent users: P50/P95/P99 latency, error rate, throughput, asyncpg pool saturation.
- Track **cost per query** (tokens × price) and **clarification precision** (how often the agent asks for clarification when the question really is ambiguous; label 50 ambiguous + 50 clear queries).
- Hosted demo (Fly.io / Render / Railway) on the sample e-commerce DB, with per-IP rate limiting.

### Phase 5: Engineering hygiene
- GitHub Actions: ruff, mypy, pytest, and a 25-question BIRD smoke eval on every PR with an accuracy regression gate.
- `make eval` → writes `results/bird_minidev_<date>.json` + a markdown table.
- README: architecture diagram, results table, ablation table, red-team table, Limitations.

## Definition of done
- [ ] BIRD Mini-Dev EX reported with per-difficulty breakdown, reproducible via one command
- [ ] Ablation table with at least 4 variants
- [ ] Red-team ASR + false-refusal rate table
- [ ] Load-test latency table at 3 concurrency levels
- [ ] Live demo + video, CI badge green

## Resume bullet templates (fill only with measured numbers)
- "Achieved **[X]% execution accuracy on BIRD Mini-Dev (PostgreSQL)**, up [Y] pts from baseline, using a 6-node LangGraph agent with schema profiling and error-classified self-correction."
- "Reduced attack success rate to **[X]%** across a [N]-prompt red-team suite with a 7-layer defense (AST validation, read-only transactions, RBAC) at a **[Y]% false-refusal rate**."
- "Sustained **P95 [X] ms** at [N] concurrent users; **$[X] per 1K queries**."
