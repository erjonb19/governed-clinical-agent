# Backlog: production readiness

The source of truth for this work. The owner edits it as plans change; work
always takes the **first unchecked item**. One item per branch and PR. If an
item is blocked, the reason goes under it and work stops.

Each item has **Done when** (how we will know) and **Today** (what already
exists, checked against the code on 2026-11-06, so nothing is built twice).

## Now, in order

- [x] **1. CI covers everything.** pytest and the FHIR eval run in CI with the hospital eval, on every push and nightly. A failing test or an eval below threshold fails the build.
  - Done when: every push to any branch runs pytest, the hospital subset gate and a **FHIR subset gate (10-15 cases, at least one of each question type)**; the nightly run executes pytest, the full hospital eval and the **full FHIR eval**; a red test or an eval below 80% turns the run red (shown by one deliberately failing run); a nightly eval whose provider is unavailable is **retried once, then turns red**.
  - Owner's decisions (2026-11-06): FHIR subset on push, full FHIR nightly (keeps Gemini's free daily quota); provider-down nightly runs fail after one retry.
  - Done 2026-11-06 (PR #53), each part seen live:
    - every push to any branch runs pytest: red on a deliberately failing test on a non-main branch (run 42140086754);
    - every push runs the FHIR 13-case subset gate: 13/13, gate PASS (run 42187179828, job fhir-quick-eval);
    - nightly pytest from the schedule: green (run 42239702871);
    - nightly evals on main: the first run after the merge (run 42238076116) ended RED, not grey: both jobs were inconclusive on Gemini's exhausted daily quota (hospital: 9 provider errors, one case unanswered; FHIR: 83/84 runs 429), retried once after 5 minutes, then failed with "Eval inconclusive twice". The FHIR job ran although the hospital job was red.
  - Learned: the nightly is scheduled 07:00 UTC but GitHub starts it around 15:00 UTC, after the day's pushes and manual sweeps have spent the shared Gemini quota (it resets 08:00 UTC). Item 3's alerts should label this `provider-outage`, not `eval-regression`.
  - Today: pytest runs only on pushes to `main` and on PRs, not nightly. The hospital eval is a 6-case subset on push and the full set nightly. The FHIR eval runs nightly and by manual dispatch, never on push. A nightly run whose provider was unavailable (exit 2) passes green with a warning.

- [x] **2. Tracing** for every agent step (plan, execute, route, retry, guard decision) with Langfuse or Arize Phoenix, self-hosted for free. If it can't run on Render's free plan, run it locally for demos and say so in the README. Show me how to open a trace for one question.
  - Done when: one question asked locally shows plan, critic, guard, execute, route and retry spans in the self-hosted tool, and the README has numbered steps to open it.
  - Today: `tracing.py` already records one trace per run and one span per step in SQLite, with a waterfall in the UI's Traces view. It is not exported to Langfuse or Phoenix, and on Render it resets with each deploy.
  - Done 2026-11-06: Arize Phoenix (Langfuse's self-hosted v3 needs Postgres, ClickHouse and Redis). `trace_export.py` exports over OTLP when `TRACE_OTLP_ENDPOINT` is set; every graph edge is now a `route` span. Phoenix runs locally only: idle it uses ~530 MB, over Render free's 512 MB. Verified live: two questions on recorded LLM responses, both traces in Phoenix with the right tree and span kinds, no result rows.

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
  - Done when: one documented setting picks the provider; when the chosen provider is down (402/429/5xx), the call automatically falls back to **Groq** (`openai/gpt-oss-120b`), and the answer records which provider served it; switching needs no code change (shown by a test and a README step).
  - Owner's decision (2026-11-06): when `PLANNER_PROVIDER` is unset the default is **Gemini**. Fallback changed 2026-11-07 from Cerebras to **Groq** (Cerebras is no longer free on the owner's account); same model, gpt-oss-120b. **Plan below awaits the owner's OK.**
  - Groq's free limits, from its own docs (console.groq.com/docs/rate-limits, "Free Plan Limits" table, read 2026-11-07), for `openai/gpt-oss-120b`: **30 requests/min, 1,000 requests/day, 8,000 tokens/min, 200,000 tokens/day**. Limits are per Groq organisation, not per key; over a limit the API answers 429 with `retry-after`, which the planner already treats as provider-unavailable.
  - What that means for the fallback: a `/query` costs two model calls (plan + critic), roughly 1.5K-3K tokens together on hospital and 3K-4.5K on FHIR (estimate below). During a Gemini outage, Groq could serve about 2 questions a minute and roughly 40-60 a day before its daily token cap. Enough for a demo; if Groq is also out, the answer says provider unavailable, as today.
  - Plan (one PR): (1) `ACTIVE_PROVIDER` default `cerebras` becomes `gemini`; (2) a fallback wrapper in `nl_to_sql_planner.py` retries a provider-unavailable call once on Groq, and `CallMetrics.provider` and the trace record who served it; `PLANNER_FALLBACK` (default `groq`, `none` turns it off); (3) Render gets `GROQ_API_KEY` as a dashboard secret (owner) and `PLANNER_FALLBACK=groq` in `render.yaml`; (4) unit tests: a 429 from Gemini is served by Groq, a 400 is not retried, and both down gives provider-unavailable; (5) README step.
  - Today: mostly built. `PLANNER_PROVIDER` already switches between gemini, cerebras, groq, xai and anthropic without code changes. If it is unset, it falls back to cerebras rather than the gemini that CI uses, and there is no automatic fallback when a provider is down.

- [ ] **9. Demo UI rebuilt for fast insight.** Leadership found the current demo clunky. The first screen should get a non-technical person to a useful answer in one or two clicks, with plain language and a short definitions section. Write the UI plan and show me before building. Add a note on the page that the first load can take a moment (Render free-plan cold start).
  - Done when: the owner has approved a written UI plan; then a first-time visitor reaches a useful answer in at most two clicks (checked by an E2E test), the first screen has a definitions section, and a cold-start note shows while the service wakes.
  - Today: the UI lands on an Overview of four fixed findings; Ask, Review, Data, Guide, Trust, Usage and Traces sit beside it as separate views.

- [ ] **10. Databricks refresh and parity check** (owner approved 2026-11-06). After `data-refresh.yml` rebuilds the hospital Gold each month, publish it to Databricks Delta and re-run the 6-case parity subset with `DATA_BACKEND=databricks`; the workflow fails on a mismatch.
  - Done when: one monthly (or dispatched) run publishes, the parity subset passes on both backends, and a mismatch turns the run red.
  - Today: verified once by hand; `scripts/publish_gold_to_databricks.py` is manual and no workflow touches Databricks, so Delta goes stale after each refresh.
  - Needs: the owner adds `DATABRICKS_SERVER_HOSTNAME`, `DATABRICKS_HTTP_PATH`, `DATABRICKS_TOKEN`, `DATABRICKS_CATALOG` and `DATABRICKS_SCHEMA` as repository secrets. $0 (Free Edition plus GitHub Actions).

- [ ] **11. The manual FHIR eval compares against main instead of requiring every case to pass** (owner, 2026-11-06; don't change it before then).
  - Done when: `eval-fhir-manual.yml` scores the chosen branch and `main` in the same workflow, and passes only if the branch's accuracy over completed runs is at least `main`'s (and still fails on an inconclusive run); the report shows both scores side by side.
  - Today: it fails unless every case passes by majority of its runs. Model noise alone can fail that bar: `main`'s own nightly on 2026-11-05 had `f4_avg_glucose_prediabetic` at 1/3, and #50's run on 2026-11-06 ended inconclusive on Gemini's daily quota.

- [ ] **12. Send the deployed app's traces to a free hosted backend** (owner, 2026-11-06), instead of running Phoenix on Render. Phoenix Cloud's free tier, falling back to Langfuse Cloud Hobby if Phoenix Cloud doesn't fit. Keep the SQLite traces as they are.
  - Done when: with the owner's two Render environment variables set, a question asked on the deploy appears in the hosted backend with its steps; the SQLite traces and Traces view are unchanged; a test shows a down, refusing or hanging backend never breaks or slows a request; what can leave the server is documented and pinned.
  - Status (2026-11-07): code, tests and setup steps in PR #55 (US region). **Phoenix Cloud no longer appears in Arize's docs** (self-hosting only; the hosted product is Arize AX, free tier 25k spans / 15 days), so Langfuse Cloud Hobby (50k units / month, 30 days, no card) is used. Owner's decision 2026-11-07: the hosted export drops the approval reason and reviewer/escalation identities and masks SQL literals; the question was to go "after the existing PHI redaction", but no free-text PHI redactor exists, so it is withheld (hash only) until the owner decides whether to build one. Remaining: the owner adds `TRACE_OTLP_ENDPOINT` and `TRACE_OTLP_HEADERS` in Render, then one live trace is checked.

- [ ] **13. Push gate on Groq, nightly stays on Gemini** (owner, 2026-11-07; Cerebras dropped the same day, no longer free). Pushes and the nightly share one Gemini free daily quota; on 2026-11-06 the nightly found it spent and ended inconclusive. **Plan below awaits the owner's OK.**
  - Compared (2026-11-07), against our prompt sizes:
    - **GitHub Models (GITHUB_TOKEN, no secret): not possible.** GitHub's own docs (docs.github.com, GitHub Models pages) say the service was **retired on 2026-07-30**: playground, catalog and inference API are no longer available to anyone.
    - **Groq free:** 30 RPM, 1K requests/day, 8K tokens/min, 200K tokens/day for `openai/gpt-oss-120b` (item 8).
  - Our prompt sizes (estimate: characters / 4 of the real system prompts; the tokenizer could not be downloaded here, and committed eval reports keep no token counts): planner system prompt **~720 tokens hospital, ~1,460 FHIR**; critic ~650 / ~1,400 (the push gate is single-shot, so no critic call); plus the question (~30) and the answer, capped at 800 (`max_tokens`; gpt-oss's reasoning counts as output).
  - What a push would cost on Groq: 6 hospital + 13 FHIR calls = **about 24K input tokens + up to 15K output, so 25K-40K per push**. So:
    - per minute: a FHIR call can reach ~2.3K tokens, so 8K TPM allows ~3 calls/min; `PLANNER_MIN_INTERVAL_SEC` must rise from 5 to **about 20 s** for Groq, and the gate takes ~6 minutes instead of ~2;
    - per day: **about 5-8 full push gates** before the 200K daily token cap. After that a push gate ends `provider_unavailable` and only warns (as today), but it no longer touches the nightly's Gemini quota, which is the point of this item;
    - requests per day (1K) do not bind.
  - **Recommendation: Groq** (the only free option left). To stretch the daily cap, a follow-up could send `reasoning_effort: low` to gpt-oss on Groq (fewer output tokens); not done until measured.
  - Shared quota: if the Render fallback (item 8) and CI use keys in the same Groq organisation, they share the 200K tokens/day. Using a separate Groq organisation for CI would avoid that; whether Groq's free plan allows a second organisation is not checked.
  - Done when: `eval-on-push.yml` runs both subsets with `PLANNER_PROVIDER=groq` and `PLANNER_MIN_INTERVAL_SEC=20` (a `GROQ_API_KEY` repository secret, added by the owner); `eval-nightly.yml` and `eval-fhir-manual.yml` stay on Gemini; the README says which provider each gate tests; one push shows completed Groq runs at or above the 80% gate. If Groq's accuracy on the subsets is below Gemini's, stop and report before switching.
  - Effect: the push gate stops testing the default provider. It still catches what does not depend on the model (guard, schema notes, prompt and harness changes, a planner that breaks for every model), but no longer catches a Gemini-only regression (a Gemini model update, the Gemini adapter, a prompt Gemini misreads); those surface at the next Gemini nightly, up to a day later. It does test the fallback model (item 8) on every push.

- [ ] **14. Remove Cerebras** (owner, 2026-11-07: no longer free on the owner's account). Every place found on 2026-11-07 (`git grep -i cerebras` on `main`; no workflow and not `render.yaml` use it):
  - `nl_to_sql_planner.py`: the module docstring (line 13, and `setx CEREBRAS_API_KEY` at line 20); the `"cerebras"` entry in `PROVIDERS` (lines 51-54); **`ACTIVE_PROVIDER` defaults to `"cerebras"` when `PLANNER_PROVIDER` is unset (line 110)**; the comment on Cerebras's tier change (line 113); `PROVIDER_RATES["cerebras"]` (line 121).
  - `eval_harness.py:344`: the `--provider` help text names cerebras.
  - `tests/integration/test_traces_api.py:35`: clears `CEREBRAS_API_KEY` in a fixture.
  - `.env.example`: lines 17 and 19 (provider list, and "unset falls back to cerebras") and `CEREBRAS_API_KEY=` (line 31).
  - `README.md`: line 36 (a sample response with `"provider": "cerebras"`, `gpt-oss-120b`), line 63 (provider list), line 619 (`gemini | groq | cerebras | xai | anthropic`).
  - `CLAUDE.md`: line 15 (provider list, and "if unset ... falls back to cerebras"), line 56 (`--provider {gemini,cerebras,...}`), line 58 ("groq/cerebras/xai/anthropic remain selectable").
  - `docs/BACKLOG.md`: items 8 and 13 (rewritten above).
  - Also stale, not Cerebras: `eval-on-push.yml:49` says the 5 s pacing is for "Groq free tier ~8K TPM", but the gate runs on Gemini.
  - Plan: drop the provider entry and its rate, so `PLANNER_PROVIDER=cerebras` fails at startup instead of calling a paid account (today an unknown provider is a bare `KeyError` at `nl_to_sql_planner.py:434`; the PR gives it a clear message); change the default to `gemini` (item 8, same line, so do that line in item 8's PR); fix docs, the README sample (re-run it, or label it as an old example), `.env.example` and CLAUDE.md; a test that `cerebras` is no longer accepted. Order: either before item 8 or folded into it, the owner's call.

## Later (keep, don't start)

- [ ] **MCP server over Streamable HTTP with real auth.**
  - Done when: MCP clients authenticate with OAuth 2.1.
  - Today: the premise is out of date. Streamable HTTP with API-key auth is **already built** (standalone and at `/mcp`, end-to-end tested on every push). What is missing is OAuth 2.1.
- [x] **Databricks lift, so the Databricks backend has actually run.** Verified once on a real Free Edition workspace (6-case eval subset at 100%, backend parity). The remaining gap, keeping it fresh and re-checked, is item 10.
- [ ] **Real users:** the owner will find three to five. Add a README section where their feedback is recorded.
  - Done when: the README has a "User feedback" section with one entry per user (date, role, what they tried, what they said).
