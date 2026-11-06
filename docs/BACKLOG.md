# Backlog: production readiness

The source of truth for this work. The owner edits it as plans change; work
always takes the **first unchecked item**. One item per branch and PR. If an
item is blocked, the reason goes under it and work stops.

Each item has **Done when** (how we will know) and **Today** (what already
exists, checked against the code on 2026-11-06, so nothing is built twice).

## Now, in order

- [ ] **1. CI covers everything.** pytest and the FHIR eval run in CI with the hospital eval, on every push and nightly. A failing test or an eval below threshold fails the build.
  - Done when: every push to any branch runs pytest, the hospital subset gate and a **FHIR subset gate (10-15 cases, at least one of each question type)**; the nightly run executes pytest, the full hospital eval and the **full FHIR eval**; a red test or an eval below 80% turns the run red (shown by one deliberately failing run); a nightly eval whose provider is unavailable is **retried once, then turns red**.
  - Owner's decisions (2026-11-06): FHIR subset on push, full FHIR nightly (keeps Gemini's free daily quota); provider-down nightly runs fail after one retry.
  - Today: pytest runs only on pushes to `main` and on PRs, not nightly. The hospital eval is a 6-case subset on push and the full set nightly. The FHIR eval runs nightly and by manual dispatch, never on push. A nightly run whose provider was unavailable (exit 2) passes green with a warning.

- [ ] **2. Tracing** for every agent step (plan, execute, route, retry, guard decision) with Langfuse or Arize Phoenix, self-hosted for free. If it can't run on Render's free plan, run it locally for demos and say so in the README. Show me how to open a trace for one question.
  - Done when: one question asked locally shows plan, critic, guard, execute, route and retry spans in the self-hosted tool, and the README has numbered steps to open it.
  - Today: `tracing.py` already records one trace per run and one span per step in SQLite, with a waterfall in the UI's Traces view. It is not exported to Langfuse or Phoenix, and on Render it resets with each deploy.

- [ ] **3. Alerts** when the nightly eval score drops below a threshold or the error rate rises. Use a free channel (email or a GitHub issue). Tell me the thresholds and why.
  - Done when: a nightly score under the threshold, or an error rate over it, opens or updates one GitHub issue, and the next healthy run closes it; the thresholds and the reasoning are in the README.
  - Owner's decision (2026-11-06): two separate labels and issues, `provider-outage` (the provider was unavailable, inconclusive) and `eval-regression` (the score really dropped), never mixed.
  - Today: `scheduled-runs.yml` and `live-check.yml` already open issues. The nightly eval opens none; a failure shows only as a red run.

- [ ] **4. Version on every answer.** Each logged answer records the prompt version, model, provider and git commit.
  - Done when: every `/query` response, its trace and its audit line carry `prompt_version`, `model`, `provider` and `git_commit`, and a test pins all four.
  - Today: model and provider are recorded per call. There is no prompt version and no git commit.

- [ ] **5. API basics:** `/v1` API versioning, semantic version tags on releases, rate limiting per API key, and a `/health` endpoint.
  - Done when: every endpoint answers under `/v1` (old paths kept working or redirected, as decided in the PR), a `vX.Y.Z` tag and GitHub release exist, and `/health` reports the version and git commit.
  - Today: per-API-key rate limiting (slowapi) and `/health` already exist. There is no `/v1` prefix, the app version is a hard-coded "1.0.0", and there are no release tags (the only tag is a data snapshot, `fhir-gold-20260901`).

- [ ] **6. Result caching in SQLite**, so repeat questions skip the LLM call. Show the hit rate. Keep in mind the cache resets when Render restarts.
  - Done when: asking the same question twice makes one LLM call, not two; the cache is keyed so a data refresh or a guard change cannot serve a stale answer, and still re-runs the guard; the hit rate shows in `/metrics`; the README says it resets on Render restarts.
  - Today: only the Overview findings are cached (per data vintage). Questions are not.

- [ ] **7. SLOs and P99 latency**, built on the latency tracking that already exists. Write the SLO targets in the README and alert when they're missed.
  - Done when: `/metrics` reports p50, p95 and p99; the README states the SLO targets; a missed SLO opens a GitHub issue (through the item 3 channel).
  - Today: `cost_tracker.py` reports per-call latency and p50/p95. There is no p99 and no SLO.

- [ ] **8. Feature flag for the planner backend** as a plain config setting (no paid service), so I can switch LLM providers without a code change.
  - Done when: one documented setting picks the provider; when the chosen provider is down (402/429/5xx), the call automatically falls back to the other of Gemini / Cerebras, and the answer records which provider served it; switching needs no code change (shown by a test and a README step).
  - Owner's decision (2026-11-06): when `PLANNER_PROVIDER` is unset the default is **Gemini**, with automatic fallback to **Cerebras** when Gemini is down.
  - Today: mostly built. `PLANNER_PROVIDER` already switches between gemini, cerebras, groq, xai and anthropic without code changes. If it is unset, it falls back to cerebras rather than the gemini that CI uses, and there is no automatic fallback when a provider is down.

- [ ] **9. Demo UI rebuilt for fast insight.** Leadership found the current demo clunky. The first screen should get a non-technical person to a useful answer in one or two clicks, with plain language and a short definitions section. Write the UI plan and show me before building. Add a note on the page that the first load can take a moment (Render free-plan cold start).
  - Done when: the owner has approved a written UI plan; then a first-time visitor reaches a useful answer in at most two clicks (checked by an E2E test), the first screen has a definitions section, and a cold-start note shows while the service wakes.
  - Today: the UI lands on an Overview of four fixed findings; Ask, Review, Data, Guide, Trust, Usage and Traces sit beside it as separate views.

- [ ] **10. Databricks refresh and parity check** (owner approved 2026-11-06). After `data-refresh.yml` rebuilds the hospital Gold each month, publish it to Databricks Delta and re-run the 6-case parity subset with `DATA_BACKEND=databricks`; the workflow fails on a mismatch.
  - Done when: one monthly (or dispatched) run publishes, the parity subset passes on both backends, and a mismatch turns the run red.
  - Today: verified once by hand; `scripts/publish_gold_to_databricks.py` is manual and no workflow touches Databricks, so Delta goes stale after each refresh.
  - Needs: the owner adds `DATABRICKS_SERVER_HOSTNAME`, `DATABRICKS_HTTP_PATH`, `DATABRICKS_TOKEN`, `DATABRICKS_CATALOG` and `DATABRICKS_SCHEMA` as repository secrets. $0 (Free Edition plus GitHub Actions).

## Later (keep, don't start)

- [ ] **MCP server over Streamable HTTP with real auth.**
  - Done when: MCP clients authenticate with OAuth 2.1.
  - Today: the premise is out of date. Streamable HTTP with API-key auth is **already built** (standalone and at `/mcp`, end-to-end tested on every push). What is missing is OAuth 2.1.
- [x] **Databricks lift, so the Databricks backend has actually run.** Verified once on a real Free Edition workspace (6-case eval subset at 100%, backend parity). The remaining gap, keeping it fresh and re-checked, is item 10.
- [ ] **Real users:** the owner will find three to five. Add a README section where their feedback is recorded.
  - Done when: the README has a "User feedback" section with one entry per user (date, role, what they tried, what they said).
