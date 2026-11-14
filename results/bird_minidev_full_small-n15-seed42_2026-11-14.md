# BIRD Mini-Dev (PostgreSQL): full_small-n15-seed42

- git: `a00f8b367d` (dirty working tree)
- models: {"planner": "openai/gpt-oss-120b", "fast": "openai/gpt-oss-20b", "fallback": "openai/gpt-oss-20b", "reasoning_effort": "low"}
- flags: {"variant": "full_small", "evidence": true, "cache": true, "cache_hits": 0, "cache_misses": 16, "concurrency": 2, "ves_repeats": 3, "max_retry_attempts": 3, "llm_provider": "groq", "profile_table_token_budget": 1500, "prompt_cap_total_tokens": 6500, "max_output_tokens": 2048, "prompt_size_estimate": {"n": 15, "cap_total_tokens": 6500, "max_total_tokens": 6480, "mean_total_tokens": 5817.4, "p95_total_tokens": 6474.4, "max_prompt_tokens": 4432, "mean_prompt_tokens": 3769.4, "p95_prompt_tokens": 4426.4, "max_correction_total_tokens": 4940, "over_cap": 0, "estimator": "ascii chars/3.5 + 1 per non-ascii char + 8 per message (conservative)"}, "pacing": {"tpm": 8000, "rpd": 1000, "requests_today": {"date": "2026-11-14", "models": {"openai/gpt-oss-120b": {"requests": 16, "rate_limited": 0, "exhausted": false}}}}, "stopped_reason": null, "remaining_questions": 0, "sql_timeout_s": 30.0, "n_questions": 15, "question_ids": [50, 87, 129, 173, 186, 440, 765, 792, 862, 896, 1042, 1091, 1115, 1148, 1149], "sample_seed": 42, "resumed_from_checkpoint": "results/bird_minidev_full_small-n15-seed42.jsonl"}
- dataset sha256: `528b01ac755784f4`, prompt sha256: `0a4a7a26bd9a90c8`
- started: 2026-11-14T11:05:08+00:00, wall: 439.0 s
- complete run: False (subset: NOT comparable to published numbers)

| run | scored/total | EX % [95% CI] | Soft F1 % | R-VES | clarify / refuse / failed % | mean attempts | latency P50 / P95 s |
|---|---|---|---|---|---|---|---|
| full_small-n15-seed42 | 15/15 | 26.7 [6.7, 53.3] | 20.6 [1.2, 40.6] | 26.7 [6.7, 53.3] | 13.3 [0.0, 33.3] / 0.0 [0.0, 0.0] / 0.0 [0.0, 0.0] | 1.08 | 58.0 / 73.6 |

## By difficulty

| difficulty | n | EX % | Soft F1 % | R-VES |
|---|---|---|---|---|
| challenging | 5 | 20.0 | 1.8 | 20.0 |
| moderate | 5 | 20.0 | 20.0 | 20.0 |
| simple | 5 | 40.0 | 40.0 | 40.0 |

## By database

| db_id | n | EX % | Soft F1 % | R-VES |
|---|---|---|---|---|
| california_schools | 2 | 0.0 | 0.0 | 0.0 |
| card_games | 1 | 0.0 | 0.0 | 0.0 |
| european_football_2 | 4 | 50.0 | 27.3 | 50.0 |
| financial | 3 | 0.0 | 0.0 | 0.0 |
| formula_1 | 2 | 50.0 | 50.0 | 50.0 |
| superhero | 2 | 50.0 | 50.0 | 50.0 |
| thrombosis_prediction | 1 | 0.0 | 0.0 | 0.0 |

## Agent behaviour

- schema linking (table recall / precision): 0.96 / 0.96; (column recall / precision): 0.68 / 0.82 (n=13, gold unparsable: 0)
- retry recovery: 0/1 (0.00) questions correct after at least one failed attempt
- tokens: {'input': 54285, 'output': 5371, 'llm_calls': 16, 'cached_calls': 0}
- model usage (who answered): {"openai/gpt-oss-120b": {"calls": 16, "cached_calls": 0, "input_tokens": 54285, "output_tokens": 5371}}; calls by role/leg: {"planner": {"primary": 16, "fallback": 0}}
- latency note: agent wall time per question (excludes scoring and infra-retry backoff sleeps; includes cache-hit calls, so cached re-runs understate it)
- R-VES: indicative only (shared database, configurable repeats; see `nl2sql.eval.metrics`)
