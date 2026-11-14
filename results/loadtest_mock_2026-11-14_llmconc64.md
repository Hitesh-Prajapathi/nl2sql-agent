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

## Analysis (mock LLM, 1 worker, in-memory sessions)

- **Knee: about 10 users (about 6 RPS).** Past it, throughput stays flat at 6.0-6.4 RPS (baseline) while P50 grows linearly with users (6.3 s at 50, 12.4 s at 100): classic queueing behind a fixed-capacity resource, with 0 errors.
- **Bottleneck: `LLM_MAX_CONCURRENCY=8`, the per-process LLM semaphore in `llm/providers.py`**, not the pool, event loop or CPU. At the baseline the pool had zero acquire wait and at least 4 idle connections, event-loop lag P95 was about 1.3 ms and server CPU was about 10% of one core. Capacity is 8 slots / mean fake planner latency (about 1.25 s) = about 6.4 RPS, which matches. In production this cap is a deliberate guard for provider rate limits, so raising it is only valid if the provider quota allows.
- **Tuning: `LLM_MAX_CONCURRENCY` 8 -> 64.** 50 users: 6.4 -> 29.8 RPS, P95 7.9 s -> 2.4 s. 100 users: 6.1 -> 55.8 RPS, P95 14.4 s -> 2.6 s. Latency is back to roughly the simulated LLM latency (the system adds little). Server CPU rose to about 42% of one core at 100 users, about 0.75% CPU per RPS.
- **Next limit (not reached):** at 100 users the pool was almost fully used (size 10, idle min 2, acquire wait P95 0 ms). A pool `max_size` change was not tried because it showed no wait yet; the first expected limit beyond about 100 users is the pool, then the single event loop / CPU core. Users here are closed-loop with 0.2-1.0 s think time, so 56 RPS is the demand of 100 users, not the system's ceiling.
- The first tuned run was discarded: the host went to sleep during it (event-loop monitor tick count was a third of expected). The runner now flags such levels (`valid`) and `make loadtest` uses `caffeinate`. The baseline JSON was rerun after the merge with the shared session store and DB hardening; both result files are from that rerun.
- **Multi-worker was not run.** The default session store is in-memory per process, so follow-ups are only correct with sticky routing. The merged code has a Postgres session backend, but it needs the `app_state` schema written to the shared database, which this task was not allowed to do. A 4-worker comparison is therefore left for when a dedicated database is available.
- Audit logging and the per-IP rate limiter were disabled for the run (audit writes would hit the shared DB; the limiter would throttle the single load-generator IP). Synthesis is templated for the small result sets in the mix, so the fast-LLM latency parameters are not exercised.
