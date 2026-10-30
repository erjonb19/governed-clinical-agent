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
