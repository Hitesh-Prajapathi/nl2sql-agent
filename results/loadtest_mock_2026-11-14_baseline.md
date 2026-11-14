# Load test (mock LLM) 2026-11-14 [baseline]

Git SHA `eee7f8c`, 1 uvicorn worker(s), 15s ramp + 60s steady state per level.

**Mock-LLM mode.** The fake LLM returns canned, valid SQL and sleeps for a sampled lognormal latency, so requests run the real validate, execute and synthesize path against Postgres. Latencies are ASSUMPTIONS until replaced with samples from real Langfuse traces: planner median 900 ms / P95 2500 ms, fast (synthesis) median 300 ms / P95 800 ms. Result sets in the mix are small, so synthesis is templated and the fast-LLM latency is not exercised.

Settings: `{"pool_min_size": 2, "pool_max_size": 10, "llm_max_concurrency": 8, "rate_limit_enabled": false, "audit_enabled": false, "traffic_mix": "70% simple / 20% complex joins / 10% 2-turn follow-ups (same thread_id)", "think_time_s": "uniform 0.2-1.0", "endpoint": "POST /v1/query"}`

Baseline: defaults (pool max_size 10, LLM_MAX_CONCURRENCY 8), in-memory sessions, 1 worker.

| Users | Valid | RPS | Err % | P50 ms | P95 ms | P99 ms | Pool size/idle-min/max | Acquire wait P95 ms | Loop lag P95 ms | Server CPU % (mean/max) | RSS MB | Locust CPU % |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | yes | 0.65 | 0.0 | 864 | 1743 | 2842 | 2/1/10 | 0.0 | 1.8 | 1.4/2.6 | 107.3 | 0.2 |
| 10 | yes | 5.73 | 0.0 | 953 | 2827 | 3762 | 3/2/10 | 0.0 | 1.5 | 7.6/11.9 | 109.8 | 0.8 |
| 50 | yes | 6.4 | 0.0 | 6318 | 7886 | 9579 | 5/4/10 | 0.0 | 1.2 | 7.9/13.8 | 115.9 | 0.8 |
| 100 | yes | 6.08 | 0.0 | 12446 | 14398 | 15504 | 8/7/10 | 0.0 | 1.3 | 9.7/16.7 | 124.5 | 1.1 |
