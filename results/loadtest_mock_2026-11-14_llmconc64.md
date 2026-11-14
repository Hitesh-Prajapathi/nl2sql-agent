# Load test (mock LLM) 2026-11-14 [llmconc64]

Git SHA `315def2`, 1 uvicorn worker(s), 15s ramp + 60s steady state per level.

**Mock-LLM mode.** The fake LLM returns canned, valid SQL and sleeps for a sampled lognormal latency, so requests run the real validate, execute and synthesize path against Postgres. Latencies are ASSUMPTIONS until replaced with samples from real Langfuse traces: planner median 900 ms / P95 2500 ms, fast (synthesis) median 300 ms / P95 800 ms. Result sets in the mix are small, so synthesis is templated and the fast-LLM latency is not exercised.

Settings: `{"pool_min_size": 2, "pool_max_size": 10, "llm_max_concurrency": 64, "rate_limit_enabled": false, "audit_enabled": false, "traffic_mix": "70% simple / 20% complex joins / 10% 2-turn follow-ups (same thread_id)", "think_time_s": "uniform 0.2-1.0", "endpoint": "POST /v1/query"}`

Tuned: LLM_MAX_CONCURRENCY 8 -> 64 (pool unchanged). Rerun of the levels at and beyond the knee.

| Users | Valid | RPS | Err % | P50 ms | P95 ms | P99 ms | Pool size/idle-min/max | Acquire wait P95 ms | Loop lag P95 ms | Server CPU % (mean/max) | RSS MB | Locust CPU % |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 10 | yes | 5.75 | 0.29 | 903 | 2769 | 3841 | 3/1/10 | 0.0 | 1.7 | 9.5/11.9 | 108.6 | 1.2 |
| 50 | NO (host stalled) | 90.0 | 100.0 | 1 | 5 | 7 | None/None/None | 0.0 | 0.0 | None/0 | 0 | None |
| 100 | NO (host stalled) | 48.8 | 100.0 | 2 | 5 | 8 | None/None/None | 0.0 | 0.0 | None/0 | 0 | None |

## Before (baseline, `{"pool_min_size": 2, "pool_max_size": 10, "llm_max_concurrency": 8, "rate_limit_enabled": false, "audit_enabled": false, "traffic_mix": "70% simple / 20% complex joins / 10% 2-turn follow-ups (same thread_id)", "think_time_s": "uniform 0.2-1.0", "endpoint": "POST /v1/query"}`) vs after

| Users | RPS before | RPS after | P50 before | P50 after | P95 before | P95 after | P99 before | P99 after | Err % before | Err % after |
|---|---|---|---|---|---|---|---|---|---|---|
| 10 | 5.65 | 5.75 | 961 | 903 | 2824 | 2769 | 3756 | 3841 | 0.0 | 0.29 |
| 50 | 6.4 | 90.0 | 6340 | 1 | 7967 | 5 | 9600 | 7 | 0.0 | 100.0 |
| 100 | 6.02 | 48.8 | 12385 | 2 | 14457 | 5 | 15235 | 8 | 0.0 | 100.0 |
