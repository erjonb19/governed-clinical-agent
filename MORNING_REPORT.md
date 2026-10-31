# Morning report — PLAN v2 build (started 2026-10-30)

One entry per merged PR. Newest last. Anthropic spend is the running total for
this build session against the $3.00 cap.

## Blocking: no Anthropic key anywhere

- `ANTHROPIC_API_KEY` is **empty in `.env`**, and the repo has **no
  `ANTHROPIC_API_KEY` Actions secret** (only CEREBRAS, GEMINI and GROQ).
- So the Haiku 5.5 live eval (hospital 42 + FHIR 28, 1 run, cap $0.50) has
  **not run**, and §0.3 has no Haiku row yet. A metered runner is ready: it
  stops before any call whose worst case could pass the cap. The worst case for
  70 single-shot calls is about $0.17.
- **You need to:** put the key in `.env` (the eval runs at the next checkpoint
  that finds it), and add the `ANTHROPIC_API_KEY` repo secret before PR 9's
  workflow can run.

## PR 2 (7d) — nightly live check of the Render deploy

- **What merged:** `live_check.py` and `.github/workflows/live-check.yml`
  (06:30 UTC nightly plus manual dispatch, $0, no key).
  - It checks that `/health` is ok, `/ui` returns 200 and the app shell
    renders, and the console shows no errors.
  - It makes 3 attempts of 90s each, 30s apart, for Render cold starts.
  - Only an all-attempts failure opens an issue (label `live-check`). An open
    issue gets a comment rather than a duplicate, and the next pass closes it.
- **A real defect it found:** the UI called `/auth-check` with an empty key on
  every first visit, which logged a 401 console error. The UI now only tests a
  key it has. The live check will fail until Render redeploys this commit.
- **Tests:** 572 passed, 1 skipped (`pytest`). Review: MERGE WITH NITS; fixed the failure-path screenshot, a non-JSON `/health`, the job timeout (now 20 min) and an "Offline" badge being overwritten.
- **Evals:** none needed (no planner change).
- **Anthropic spend so far:** $0.00.
- **You need to:** nothing for this PR.

## PR 7 (item 5 + 7b) — MCP over Streamable HTTP, at `/mcp` and standalone (#33)

- **Order:** merged ahead of PR 4, deliberately. PR 7 depends on nothing, and
  PR 4 (#32, critic + D2) is waiting on its §10 accuracy measurement. Gemini's
  free daily quota ran out yesterday afternoon and resets about 07:00 UTC; the
  interim measurement is running on Groq.
- **What merged:**
  - `python mcp_server.py --transport http` serves Streamable HTTP (stateless
    JSON), and the API serves the same tools at `/mcp` (D5). stdio is unchanged.
  - **Auth is an API key, not OAuth 2.1.** It is sent as `X-API-Key` or Bearer,
    and comes from `MCP_API_KEY`, else `API_KEY` (D6).
  - Without a key the HTTP transport refuses to start.
  - Browser Origins must be localhost or listed in `MCP_ALLOWED_ORIGINS`.
  - Rate limit: 60 calls per minute per key.
  - Mounted, MCP calls share the API's audit log and DuckDB lock, and run on a
    worker thread, so they can never freeze the event loop.
  - `mcp-e2e.yml` runs on every push: a real MCP client over stdio, standalone
    HTTP and `/mcp`, covering list tools, an allowed query, a blocked query,
    401s and both header forms.
- **Reviews:** 3 rounds. They found:
  - `/mcp` was not audited and not under the DuckDB lock;
  - `/mcp` had no rate limit;
  - a sync tool blocked the event loop (I reproduced a 3.5s `/health` stall).
  All fixed, each with a test that fails without the fix.
- **Tests:** 590 passed, 1 skipped (`pytest`). MCP E2E 7/7 locally and in CI.
- **Evals:** none needed (no planner change).
- **Anthropic spend so far:** $0.00. There is still no key.
- **You need to (optional):** set `MCP_API_KEY` on Render for a separate,
  rotatable MCP key. Without it, `/mcp` uses `API_KEY`. The MCP URL is
  `https://governed-clinical-agent.onrender.com/mcp`.

## PR 4 (item 4 + D2) — the critic in both graphs; `/query` on the graph (#32)

- **What merged:**
  - **The critic.** `critic.py` makes one model call between plan and execute,
    in both graphs.
    - `revise` re-plans and costs an attempt.
    - Any error is `unavailable` and goes on to execute (D4).
    - It never informs or skips the guard; a test shows the guard still denies
      what the critic approved.
    - Each verdict is recorded in the trajectory, a `critic` span and an audit
      `critic_review` event.
    - `CRITIC_ENABLED` and `CRITIC_ROUTING` switch it.
  - **`/query` runs on the graph** (`QUERY_MODE=graph`, the default; `single`
    keeps the old path).
    - The response shape is the same, plus `attempts`, `critic` and `trajectory`.
    - Every plan and critic call goes into the cost log.
- **§10 measurement:** recorded in PLAN_v2 §0.3. It ran on Groq, because
  Gemini's daily quota was gone.
  - Routing on: hospital 42/42, FHIR 19/19 completed (9 runs were Groq 429s).
  - Record-only: 70/70.
  - The critic does not lower accuracy, so routing stays on. I'll confirm on
    Gemini after its quota resets.
- **Reviews:** 4 rounds. They found:
  - **(blocker)** the critic didn't know the `unanswerable` refusal shape;
  - **(blocker)** DuckDB execution errors quoted data values (a FHIR birth date
    through `strptime`, a city in a DATE cast) into checkpoints, traces and the
    retry prompt sent to the LLM. Now any data-value error is reduced to its
    class plus target type. Checked against about 120 crafted queries on the
    real FHIR Gold;
  - a value that looked like a type name;
  - Databricks SQLSTATE 22 errors;
  - planner and SQL failures shown as guard refusals;
  - cost of failed runs not recorded.
  All fixed, each with a test that fails without the fix.
- **Tests:** 635 passed, 1 skipped (`pytest`). MCP E2E 7/7.
- **Evals:** Gemini's gates are unchanged (single-shot). The graph numbers are
  in §0.3 above.
- **Anthropic spend so far:** $0.00. There is still no key.
- **Render:** no new env var. **Latency:** a graph `/query` makes at least two
  paced Gemini calls, so expect 5–10 s or more per answer on the free tier.

## PR 5 (item 3) — trajectory scoring and 10 adversarial cases (#34)

- **What merged:**
  - `trajectory_eval.py` scores the whole path of each graph run on answer,
    tables, retries, guard, stopping and critic. It is deterministic.
  - The guard dimension re-runs `sql_guard` on every attempted SQL. Anything
    the guard denies must have been refused before it reached the database.
  - `--trajectory` runs this on the existing banks. `EVAL_DATASET=adversarial`
    runs 10 cases (prompt injection, PHI exfiltration, allowlist bypass) with a
    **100% gate** on guard, stopping and tables.
  - The existing gates and exit codes are unchanged. Trajectory suites run live
    only in 7c.
- **⚠ A finding you need to decide on:** `sql_guard` **allows**
  `SELECT patient_id, birth_date, postal_code FROM gold_patient`, a row-level
  quasi-identifier dump capped at 1,000 rows.
  - `adv_phi_quasi_ids` states the behaviour we want and is marked `known_gap`.
    It fails whenever the model complies. A test pins that the gap is real today.
  - The rules for this run forbid me changing `sql_guard`, so this is your call.
    The options are: deny row-level access to those three columns together,
    require aggregation on `gold_patient`, or accept and document it.
- **Live check:** I ran it on Groq twice. Both runs were inconclusive: Groq's
  limit returned 429s and only 1 of 10 cases completed each time. The two
  completed cases (ignore-rules, catalog probe) were both safe. A full run is
  for Gemini after its quota resets, or 7c.
- **Reviews:** 3 rounds. They found:
  - policy-layer denials counted as misses;
  - `deny_rule` required every denial to match;
  - a missing FHIR Gold made 7 cases pass without testing anything;
  - `tables_in` blind to table functions and DDL;
  - **(blocker)** my first fix let a guard-denied query that reached DuckDB and
    then errored count as safe.
  All fixed, each with a test that fails without the fix, including one through
  the real graph, runtime and guard.
- **Tests:** 659 passed, 1 skipped.
- **Anthropic spend so far:** $0.00. There is still no key.
- **You need to:** decide the quasi-identifier gap (above).

## Guard hotfix — escapes around the whole guard (#37)

- **Why a second guard PR:** the PHI reviews found holes that bypassed the
  entire guard, not just the PHI rules. You approved closing them separately.
  They are recorded in PLAN_v2 §0.5 and CLAUDE.md.
  - SQL-string runners: `query()`, `query_table()`, `json_execute_serialized_sql`.
  - File and metadata readers: FROM-clause table functions are now an
    allowlist, and reader families are denied by prefix.
  - CTE shadowing, including `WITH RECURSIVE`.
  - Checkpoint writes: denied, and the connection is now read-only.
- **Reviews:** 2 rounds. The second found `json_serialize_plan('<sql>')`, a
  string binder that leaked bronze schemas, reached files, and worked as a
  statistics oracle. Fixed by denying the whole `json_serialize*` /
  `json_deserialize*` family, with 2 tests that fail without the fix. About 75
  other probes held.
- **Evals:** all 70 reference queries (42 hospital + 28 FHIR) pass the guard on
  the read-only connection.
- **Open, not introduced here:** a non-terminating recursive CTE can hang the
  connection. There is no statement timeout yet.
- **CI caught one I missed:** the read-only connection broke the eval harness.
  DuckDB refuses a read-write and a read-only connection to the same file in
  one process, and the harness's reference backend held the read-write one.
  `LocalDuckDBBackend` now opens files read-only too. A test opens both
  connections on one file and fails without the fix.
- **Tests:** 692 passed, 1 skipped.
- **Anthropic spend so far:** $0.00.
- **Render:** no new env var.
