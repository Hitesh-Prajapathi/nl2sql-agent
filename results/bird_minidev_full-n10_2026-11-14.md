# BIRD Mini-Dev (PostgreSQL): full-n10

- git: `bfc61b5414` (dirty working tree)
- models: {"planner": "qwen/qwen3.8-27b", "fast": "qwen/qwen3.8-27b", "fallback": "openai/gpt-oss-20b"}
- flags: {"variant": "full", "evidence": true, "cache": true, "cache_hits": 0, "cache_misses": 21, "concurrency": 1, "ves_repeats": 3, "max_retry_attempts": 3, "llm_provider": "groq", "profile_table_token_budget": 1500, "sql_timeout_s": 30.0, "n_questions": 10, "question_ids": [5, 340, 531, 1471, 1025, 89, 846, 1312, 717, 1149], "resumed_from_checkpoint": "results/bird_minidev_full-n10_2026-11-14.jsonl"}
- dataset sha256: `528b01ac755784f4`, prompt sha256: `0a4a7a26bd9a90c8`
- started: 2026-11-14T08:48:28+00:00, wall: 326.0 s
- complete run: False (subset: NOT comparable to published numbers)

| run | scored/total | EX % [95% CI] | Soft F1 % | R-VES | clarify / refuse / failed % | mean attempts | cost | latency P50 / P95 s |
|---|---|---|---|---|---|---|---|---|
| full-n10 | 8/10 | 62.5 [25.0, 100.0] | 70.8 [37.5, 100.0] | 59.1 [25.0, 91.6] | 0.0 [0.0, 0.0] / 0.0 [0.0, 0.0] / 0.0 [0.0, 0.0] | 1.12 | n/a (no price configured) | 2.2 / 4.1 |

WARNING: 2 question(s) hit LLM infrastructure errors and are EXCLUDED from all rates: ids [846, 1025]

## By difficulty

| difficulty | n | EX % | Soft F1 % | R-VES |
|---|---|---|---|---|
| moderate | 2 | 50.0 | 50.0 | 43.3 |
| simple | 6 | 66.7 | 77.8 | 64.4 |

## By database

| db_id | n | EX % | Soft F1 % | R-VES |
|---|---|---|---|---|
| california_schools | 1 | 100.0 | 100.0 | 100.0 |
| card_games | 1 | 0.0 | 0.0 | 0.0 |
| codebase_community | 1 | 0.0 | 66.7 | 0.0 |
| debit_card_specializing | 1 | 100.0 | 100.0 | 100.0 |
| financial | 1 | 100.0 | 100.0 | 86.6 |
| student_club | 1 | 100.0 | 100.0 | 100.0 |
| superhero | 1 | 100.0 | 100.0 | 86.6 |
| thrombosis_prediction | 1 | 0.0 | 0.0 | 0.0 |

## Agent behaviour

- schema linking (table recall / precision): 1.00 / 1.00; (column recall / precision): 0.89 / 0.92 (n=8, gold unparsable: 0)
- retry recovery: 1/1 (1.00) questions correct after at least one failed attempt
- tokens: {'input': 42461, 'output': 2912, 'llm_calls': 9, 'cached_calls': 0}
- cost note: tokens counted for every call including cache hits (as if paid); no price configured for: qwen/qwen3.8-27b
- latency note: agent wall time per question (excludes scoring and infra-retry backoff sleeps; includes cache-hit calls, so cached re-runs understate it)
- R-VES: indicative only (shared database, configurable repeats; see `nl2sql.eval.metrics`)
