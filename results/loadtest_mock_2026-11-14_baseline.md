# Load test (mock LLM) 2026-11-14 [baseline]

Git SHA `315def2`, 1 uvicorn worker(s), 15s ramp + 60s steady state per level.

**Mock-LLM mode.** The fake LLM returns canned, valid SQL and sleeps for a sampled lognormal latency, so requests run the real validate, execute and synthesize path against Postgres. Latencies are ASSUMPTIONS until replaced with samples from real Langfuse traces: planner median 900 ms / P95 2500 ms, fast (synthesis) median 300 ms / P95 800 ms. Result sets in the mix are small, so synthesis is templated and the fast-LLM latency is not exercised.

Settings: `{"pool_min_size": 2, "pool_max_size": 10, "llm_max_concurrency": 8, "rate_limit_enabled": false, "audit_enabled": false, "traffic_mix": "70% simple / 20% complex joins / 10% 2-turn follow-ups (same thread_id)", "think_time_s": "uniform 0.2-1.0", "endpoint": "POST /v1/query"}`

Baseline: defaults (pool max_size 10, LLM_MAX_CONCURRENCY 8).

| Users | RPS | Err % | P50 ms | P95 ms | P99 ms | Pool size/idle-min/max | Acquire wait P95 ms | Loop lag P95 ms | Server CPU % (mean/max) | RSS MB | Locust CPU % |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 0.67 | 0.0 | 867 | 1760 | 2849 | 2/1/10 | 0.0 | 1.6 | 1.5/2.6 | 106.3 | 0.2 |
| 10 | 5.65 | 0.0 | 961 | 2824 | 3756 | 3/2/10 | 0.0 | 1.8 | 11.2/14.7 | 108.6 | 1.5 |
| 50 | 6.4 | 0.0 | 6340 | 7967 | 9600 | 5/3/10 | 0.0 | 1.6 | 12.6/17.2 | 114.8 | 1.6 |
| 100 | 6.02 | 0.0 | 12385 | 14457 | 15235 | 8/6/10 | 0.0 | 2.7 | 13.9/18.8 | 123.3 | 1.8 |
