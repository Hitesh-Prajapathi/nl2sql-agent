# Load test (mock LLM), 1 vs 4 workers, shared Postgres sessions, 2026-11-15

Base commit `949fb8a` (plus the runner changes in the same commit). Same profile as the 2026-11-14 runs: 15s ramp + 60s steady per level, closed-loop users, think time 0.2-1.0 s, mix 70% simple / 20% complex joins / 10% 2-turn follow-ups.

**Mock-LLM mode.** The fake LLM returns canned, valid SQL and sleeps a sampled lognormal latency (planner median 900 ms / P95 2500 ms; these are assumptions, not measurements). Requests run the real validate, execute and session path against the shared Postgres.

Settings: `SESSION_BACKEND=postgres`, `LLM_MAX_CONCURRENCY=32` per process (so 4 workers allow 128 in flight), pool max 10 per process, audit log and per-IP rate limiter disabled. The 4-worker run used a multiprocess Prometheus directory (`PROMETHEUS_MULTIPROC_DIR`, temp dir). Load generator and server share one laptop, and a BIRD evaluation was using the same Postgres at the time.

## Results

| Users | RPS 1w | RPS 4w | P50 1w | P50 4w | P95 1w | P95 4w | P99 1w | P99 4w | Err % 1w | Err % 4w | Server CPU % mean/max 1w | 4w |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 10 | 5.52 | 5.87 | 897 | 898 | 2786 | 2715 | 3842 | 3794 | 0.0 | 0.0 | 11.0/14.5 | 15.3/19.2 |
| 50 | 28.48 | 28.88 | 1002 | 932 | 2557 | 2696 | 3603 | 3762 | 0.0 | 0.0 | 44.9/52.5 | 52.4/60.4 |
| 100 | 27.65 | 58.88 | 2732 | 913 | 4223 | 2584 | 5575 | 3625 | 0.0 | 0.0 | 47.7/54.1 | 109.5/133.4 |

All levels valid (no host stalls). Zero errors in every run. Latencies in ms.

## Follow-up check (`--verify-followups`)

After the load levels, 40 fresh two-turn conversations were sent over new connections (so they spread across workers), then `app_state.sessions` was read directly.

- 1 worker: 40/40 HTTP pairs ok, 40/40 threads hold both turns in the store.
- 4 workers: 40/40 HTTP pairs ok, 40/40 threads in the store, 40/40 with both turns.

So with the Postgres session backend, both turns of a follow-up land in the shared store regardless of which worker served each turn. This verifies the store and write path. It does not prove the second answer was resolved using the first turn's context (the mock LLM returns canned SQL).

## Interpretation

- **10 and 50 users: no difference** (5.5 vs 5.9 RPS; 28.5 vs 28.9 RPS). One worker was not the limit at these loads.
- **100 users: 27.7 RPS and P50 2.7 s with 1 worker vs 58.9 RPS and P50 0.9 s with 4 workers.** With one process the per-process LLM semaphore (32 slots / about 1.25 s mean planner latency, about 26 RPS) saturates and requests queue. With 4 workers the aggregate is 128 slots, latency returns to roughly the simulated LLM latency, and throughput matches the demand of 100 users (about the 56 RPS seen when the cap was raised to 64 on 2026-11-14).
- **What this shows:** the multi-worker deployment works end to end (shared Postgres sessions, multiprocess metrics, zero errors) and multiplies the per-process concurrency cap. The Postgres session store adds no visible latency at this load (1-worker numbers match the earlier in-memory runs at 10 and 50 users).
- **What this does not show:** the gain at 100 users comes mainly from 4x the aggregate `LLM_MAX_CONCURRENCY`, which a single process could also get by raising the cap (the 2026-11-14 tuning did that). It is not evidence of 4x CPU scaling: the single worker used about 48% of a core, so CPU was never its limit. With a real provider the aggregate cap is bounded by the provider quota, so extra workers would not add throughput past that quota. Users are closed-loop, so about 59 RPS is the demand of 100 users, not the ceiling of 4 workers. One run per configuration, so differences of a few percent are noise. Server and load generator share one laptop and a BIRD eval was using the same database.
- **Resource overhead:** peak RSS rose from about 290 MB to about 1.1 GB (4 processes) and mean CPU from about 48% to about 110% of a core at 100 users.
- Pool columns in the JSON come from the metrics endpoint; under the multiprocess setup treat the 4-worker pool numbers as indicative only. Connections: 4 workers x pool max 10 against Postgres `max_connections=100`, shared with other users of that database.
