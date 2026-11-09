# Benchmarks

Measured, never assumed (BACKLOG rules for items 15-27): every number names
the machine, the mode and the command. All runs use **recorded model answers**
(`PLANNER_PROVIDER=replay`), so they measure this stack -- the graph, the guard,
DuckDB, tracing, the lock -- not a model provider, and cost $0.

## /query under concurrency -- baseline, before item 20 (2026-11-09)

`python scripts/load_test.py --requests 200`: the real API (`uvicorn app:app`,
one worker) on the committed hospital Gold, `QUERY_MODE=graph` (plan, critic,
guard, execute per request), answer cache off, rate limiter raised for the run.
Every request asks the recorded question "How many hospitals have an overall
star rating of exactly 5?". Machine: the overnight session's Linux sandbox, 4
CPUs, Python 3.13.

| concurrent users | requests | ok | p50 | p95 | p99 | throughput |
|---|---|---|---|---|---|---|
| 1  | 200 | 200 | 26.2 ms  | 30.3 ms  | 33.7 ms  | 38.0 req/s |
| 5  | 200 | 200 | 120.1 ms | 144.6 ms | 153.9 ms | 41.0 req/s |
| 20 | 200 | 200 | 494.3 ms | 534.1 ms | 543.6 ms | 40.2 req/s |

**The bottleneck is visible in the shape, not just the numbers:** throughput
stays flat at about 40 requests a second whatever the concurrency, and latency
grows in step with the number of users (about 25 ms x users). That is the
shape of a queue. What is known to be serialised: every DuckDB execution runs
under one global lock (`app.py`, `_LOCK`; in the graph path it wraps
`runtime.execute_tool`, `agent_graph.py`). What is NOT under that lock: the
plan and critic calls, so with a real model, whose calls take seconds,
concurrent requests do overlap on the slow part. On replayed answers the model
step is near-instant, so this table measures everything else. It does not prove
the lock is the only cap: the GIL, the SQLite tracer and the checkpointer may
also contribute. Part 2 will show how much the lock alone accounts for.

Percentiles: p95/p99 are nearest-rank, p50 is the interpolated median.

Not yet measured: the same on a GitHub runner (`load-test.yml`, dispatch only).

## After item 20 part 2 (2026-11-09)

Same command, same sandbox, same day. Measured in two steps, so each change
shows what it did on its own:

| step | users | p50 | p95 | p99 | throughput |
|---|---|---|---|---|---|
| baseline (above) | 1 / 5 / 20 | 26.2 / 120.1 / 494.3 ms | 30.3 / 144.6 / 534.1 ms | 33.7 / 153.9 / 543.6 ms | 38.0 / 41.0 / 40.2 req/s |
| 1. global lock replaced by a DuckDB cursor per query | 1 / 5 / 20 | 26.6 / 106.4 / 497.7 ms | 32.1 / 177.6 / 598.4 ms | 36.4 / 193.7 / 624.4 ms | 37.0 / 42.8 / 39.6 req/s |
| 2. + one long-lived WAL writer for the tracer | 1 / 5 / 20 | 16.3 / 89.7 / 360.1 ms | 22.0 / 107.8 / 486.6 ms | 24.4 / 126.3 / 494.8 ms | 59.5 / 55.5 / 53.7 req/s |

**What this shows (and what it does not):**
- **The lock was not the cap.** Replacing it changed nothing measurable on its
  own (step 1, within run-to-run noise). With replayed answers DuckDB execution
  is about 1 ms of a 25 ms request, so serialising it cost little. The change
  still matters with a real model: queries, `/raw-sql` and `/mcp` no longer
  wait behind each other, and `/propose`, which held the lock across a model
  call, no longer blocks anyone's queries (it now serialises only with other
  approval runs).
- **The tracer was a real queue.** A profile of one request (cProfile, 30
  requests) put about 18 ms of it in `tracing._write`: a new SQLite connection,
  a full-sync commit and a close per span, about ten spans a run, all under one
  lock. One long-lived writer with WAL and `synchronous=NORMAL` (step 2) cut
  single-user p50 from 26 to 16 ms and raised throughput from ~40 to ~55 req/s.
- **What is left is Python CPU in one process.** Throughput is still flat as
  users increase (55-60 req/s from 1 to 20 users): the graph, the guard's SQL
  parsing and the PHI checks are pure Python under the GIL, so one uvicorn
  worker uses about one core. More throughput needs more processes (item 24,
  two replicas), which needs the state in Postgres first (item 19).
- Runs that call the model are capped at `MAX_CONCURRENT_RUNS` (default 4);
  past `RUN_QUEUE_WAIT_SEC` (default 15 s) a caller gets 429 with
  `Retry-After: 5`. This table does not exercise that limit: its requests
  finish in milliseconds.
