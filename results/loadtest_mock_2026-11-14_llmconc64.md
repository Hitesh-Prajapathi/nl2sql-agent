# Load test (mock LLM) 2026-11-14 [llmconc64]

Git SHA `eee7f8c`, 1 uvicorn worker(s), 15s ramp + 60s steady state per level.

**Mock-LLM mode.** The fake LLM returns canned, valid SQL and sleeps for a sampled lognormal latency, so requests run the real validate, execute and synthesize path against Postgres. Latencies are ASSUMPTIONS until replaced with samples from real Langfuse traces: planner median 900 ms / P95 2500 ms, fast (synthesis) median 300 ms / P95 800 ms. Result sets in the mix are small, so synthesis is templated and the fast-LLM latency is not exercised.

Settings: `{"pool_min_size": 2, "pool_max_size": 10, "llm_max_concurrency": 64, "rate_limit_enabled": false, "audit_enabled": false, "traffic_mix": "70% simple / 20% complex joins / 10% 2-turn follow-ups (same thread_id)", "think_time_s": "uniform 0.2-1.0", "endpoint": "POST /v1/query"}`

Tuned: LLM_MAX_CONCURRENCY 8 -> 64 (pool unchanged).

| Users | Valid | RPS | Err % | P50 ms | P95 ms | P99 ms | Pool size/idle-min/max | Acquire wait P95 ms | Loop lag P95 ms | Server CPU % (mean/max) | RSS MB | Locust CPU % |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 10 | yes | 5.82 | 0.0 | 914 | 2777 | 3841 | 3/2/10 | 0.0 | 2.3 | 8.5/12.2 | 108.8 | 1.0 |
| 50 | yes | 29.8 | 0.0 | 903 | 2428 | 3462 | 5/3/10 | 0.0 | 2.1 | 28.5/30.6 | 114.9 | 2.8 |
| 100 | yes | 55.77 | 0.0 | 1024 | 2569 | 4092 | 10/2/10 | 0.0 | 2.0 | 41.6/45.3 | 122.4 | 4.1 |

## Before (baseline, `{"pool_min_size": 2, "pool_max_size": 10, "llm_max_concurrency": 8, "rate_limit_enabled": false, "audit_enabled": false, "traffic_mix": "70% simple / 20% complex joins / 10% 2-turn follow-ups (same thread_id)", "think_time_s": "uniform 0.2-1.0", "endpoint": "POST /v1/query"}`) vs after

| Users | RPS before | RPS after | P50 before | P50 after | P95 before | P95 after | P99 before | P99 after | Err % before | Err % after |
|---|---|---|---|---|---|---|---|---|---|---|
| 10 | 5.73 | 5.82 | 953 | 914 | 2827 | 2777 | 3762 | 3841 | 0.0 | 0.0 |
| 50 | 6.4 | 29.8 | 6318 | 903 | 7886 | 2428 | 9579 | 3462 | 0.0 | 0.0 |
| 100 | 6.08 | 55.77 | 12446 | 1024 | 14398 | 2569 | 15504 | 4092 | 0.0 | 0.0 |
