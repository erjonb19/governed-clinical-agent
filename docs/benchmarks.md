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
grows in step with the number of users (about 25 ms x users). That is a
queue: every query runs under one global lock (`app.py`, `_LOCK`), so the API
answers one question at a time. With a real model the per-request time is
seconds, not 25 ms, so the queue is that much longer.

Not yet measured: the same on a GitHub runner (`load-test.yml`, dispatch only),
and the "after" numbers, which come with the lock change (item 20, part 2).

## What item 20 part 2 changes, and how it will be checked

- Replace the global lock with a read-only DuckDB cursor per request (DuckDB
  connections are not safe to share across threads; cursors from one
  connection are), keep the per-statement timeout working per cursor.
- Bound concurrent model calls with a semaphore; return 429 with
  `Retry-After` when the queue is full.
- Re-run this table; a test runs denied and allowed queries in parallel and
  checks every decision and audit line.
