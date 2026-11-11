# Backlog: production readiness

The source of truth for this work. The owner edits it as plans change; work
always takes the **first unchecked item in the "Order of work" below** (item
numbers never change; the order is set separately). One item per branch and PR.
If an item is blocked, the reason goes under it; work stops, or, in an
unattended run the owner has started, moves on to the next item that does not
depend on it.

Each item has **Done when** (how we will know) and **Today** (what already
exists, checked against the code on 2026-11-06, so nothing is built twice).

## Order of work (owner, 2026-11-08)

**First (owner, 2026-11-09): item 28, the security audit** (plan only), after any PR already in progress. The items it adds are placed by the owner when he merges it.

1. **Open PRs first.** #40 (age cap): bring it up to date with `main` (which now has #50),
   run its full FHIR eval on a GitHub runner, and **ask the owner before merging**. #46
   (de-identified FHIR layer, PR A) stays as it is unless the owner says otherwise.
2. **8, with 14 folded into the same PR** (Groq fallback, Cerebras removed).
3. **13**, rewritten 2026-11-08 below (replayed push gate plus a small live Groq check).
4. **3, 4, 5** (alerts, version on every answer, API basics and releases).
5. **15, 16, 17** (code quality gates, supply chain and merge rules, a container image).
6. **18** (package layout): write the plan only, then wait for the owner's OK.
7. **6** (caching), then **19** (durable state in Postgres), then **20** (concurrency and a
   load test), then **7** (SLOs, with targets from 20's measured numbers).
8. **11, 22, 23, 24, 21, 25** (manual eval vs main, users and roles, free-text PHI
   redaction, the feedback loop, two replicas, RAG over measure definitions).
9. **27** (one-page architecture and a recorded walkthrough) last.

**Waiting on the owner**, built as soon as they are unblocked: **9** (the owner approves the
written UI plan first), **10** (Databricks secrets), **12** (the Render variables for
Langfuse), **26** (the data intake platform's Stage 22).

**Rules for the new items (15 to 27), owner 2026-11-08:**
- Anything marked **(ask)** waits for the owner: a new account or hosted service, anything
  that could bill, a change to the other repo, or publishing a release.
- **Render budget.** The free plan has 512 MB. Anything added to the deploy (an image, a
  library, a model) has its idle and peak memory measured in its PR; above about 400 MB
  idle it does not ship to Render and stays local-only, said so in the README.
- **Scale claims are measured, never assumed**: "p95 X ms at N concurrent users on a GitHub
  runner, with recorded LLM answers", not "production scale".
- Load tests and replays use `PLANNER_PROVIDER=replay` (the committed recordings the UI E2E
  already uses), so they cost $0 and spend no free quota.

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
  - **Status 2026-11-09 (overnight run): built, in the PR "Item 3".** `nightly_alerts.py` runs after each nightly job with the gate's exit code and the newest report: `eval-regression` (accuracy under the 80% gate, or the harness broke) or `provider-outage` (inconclusive after the retry: >20% of runs never reached the provider, a case answered fewer than 2 times, or an attempt timed out); one issue per kind per dataset, a repeat comments, a healthy night closes both, an outage leaves a regression issue alone, a scored night closes an outage issue. Thresholds and reasons are in the README (CI section). Covered by `tests/unit/test_nightly_alerts.py`; **not yet seen live** -- tick after the first nightly that opens or closes an issue.

- [x] **4. Version on every answer.** Each logged answer records the prompt version, model, provider and git commit.
  - Done when: every `/query` response, its trace and its audit line carry `prompt_version`, `model`, `provider` and `git_commit`, and a test pins all four.
  - Today: model and provider are recorded per call. There is no prompt version and no git commit.
  - **Done 2026-11-09 (overnight run), PR "Item 4".** `versioning.py`: `prompt_version` is `p-` + 12 hex of sha256 over the exact system and critic prompts for the dataset (derived, so it cannot be forgotten); `git_commit` from `RENDER_GIT_COMMIT`, `GITHUB_SHA`, `GIT_COMMIT`, then `git rev-parse HEAD`, else `unknown`; `model`/`provider` of the last plan call (the one that ran). On the `/query` response (`version`), the trace's attrs, and a new `answer` audit event, on both query modes and on planner errors. Pinned by `test_every_answer_carries_its_version_in_response_trace_and_audit` (graph and single).

- [ ] **5. API basics:** `/v1` API versioning, semantic version tags on releases, rate limiting per API key, and a `/health` endpoint.
  - Done when: every endpoint answers under `/v1` (old paths kept working or redirected, as decided in the PR), a `vX.Y.Z` tag and GitHub release exist, and `/health` reports the version and git commit.
  - Today: per-API-key rate limiting (slowapi) and `/health` already exist. There is no `/v1` prefix, the app version is a hard-coded "1.0.0", and there are no release tags (the only tag is a data snapshot, `fhir-gold-20260901`).
  - **Status 2026-11-09 (overnight run): built, in the PR "Item 5".** Every path answers under `/v1` too (an ASGI middleware maps `/v1/<path>` onto the one route set, so the old paths keep working and the two cannot drift; decided in the PR); `API-Version: v1` on every response; `VERSION` (now `1.1.0`) is the single source of the app version (FastAPI and `/health`); `/health` reports `version`, `api_version` and `git_commit`; `release.yml` turns a pushed `vX.Y.Z` tag that matches `VERSION` into a GitHub release after pytest passes. **Waiting on the owner (ask, publishing):** push the first tag, `git tag v1.1.0 && git push origin v1.1.0`, after this PR merges; the item is ticked once that release exists. Noticed, not changed: `/metrics` answers without the API key (it was like that before).

- [x] **6. Result caching in SQLite**, so repeat questions skip the LLM call. Show the hit rate. Keep in mind the cache resets when Render restarts.
  - Done when: asking the same question twice makes one LLM call, not two; the cache is keyed so a data refresh or a guard change cannot serve a stale answer, and still re-runs the guard; the hit rate shows in `/metrics`; the README says it resets on Render restarts.
  - Today: only the Overview findings are cached (per data vintage). Questions are not.
  - **Done 2026-11-09 (overnight run), PR "Item 6".** `llm_cache.py` caches the model's TEXT (plan SQL, critic verdict), never rows or decisions, keyed on `replay.request_key` (role, system-prompt hash, user text) + provider + model + data vintage (`REFRESH.json`) + guard version (hash of `sql_guard.py`, `phi_guard.py`, `phi_columns.yaml`); a hit still goes through the guard, the query and `check_output`. `LLM_CACHE`, `LLM_CACHE_DB`, `LLM_CACHE_TTL_SEC`; `/metrics` -> `llm_cache` (hits, misses, hit_rate, entries); cached calls are `cached: true`, $0, and marked on the plan span. Off in the eval harness, scheduled runs and the test suite (an autouse fixture). Pinned by `tests/unit/test_llm_cache.py`.

- [x] **7. SLOs and P99 latency**, built on the latency tracking that already exists. Write the SLO targets in the README and alert when they're missed.
  - Done when: `/metrics` reports p50, p95 and p99; the README states the SLO targets; a missed SLO opens a GitHub issue (through the item 3 channel).
  - Today: `cost_tracker.py` reports per-call latency and p50/p95. There is no p99 and no SLO.
  - **Done 2026-11-09, in the PR "Item 7".**
    - `/metrics` reports p50, p95 and p99.
    - The README's "Service level objectives" table gives the stack-latency targets, which come from item 20's measurements: p95 <= 150 ms at 1 user; p95 <= 400 ms and p99 <= 600 ms at 5 users; no failed requests. It also covers availability (`live-check.yml`) and says that model latency is reported but has no target.
    - `load-test.yml` runs weekly and then runs `slo.py --alert`. A miss opens one `slo-miss` issue, a repeat comments on it, and the first run that meets every target closes it. The issue logic is unit-tested.
    - **Seen on a runner:** run 42657985385, 5 users at p95 106 ms and p99 136 ms. SLO met, so no issue was opened.

- [x] **8. Feature flag for the planner backend** as a plain config setting (no paid service), so I can switch LLM providers without a code change.
  - Done when: one documented setting picks the provider; when the chosen provider is down (402/429/5xx), the call automatically falls back to **Groq** (`openai/gpt-oss-120b`), and the answer records which provider served it; switching needs no code change (shown by a test and a README step).
  - Owner's decision (2026-11-06): when `PLANNER_PROVIDER` is unset the default is **Gemini**. Fallback changed 2026-11-07 from Cerebras to **Groq** (Cerebras is no longer free on the owner's account); same model, gpt-oss-120b. **Plan approved by the owner 2026-11-08, with item 14 folded into the same PR.** The owner adds `GROQ_API_KEY` in Render (and as a repository secret for item 13).
  - Groq's free limits, from its own docs (console.groq.com/docs/rate-limits, "Free Plan Limits" table, read 2026-11-07), for `openai/gpt-oss-120b`: **30 requests/min, 1,000 requests/day, 8,000 tokens/min, 200,000 tokens/day**. Limits are per Groq organisation, not per key; over a limit the API answers 429 with `retry-after`, which the planner already treats as provider-unavailable.
  - What that means for the fallback: a `/query` costs two model calls (plan + critic), roughly 1.5K-3K tokens together on hospital and 3K-4.5K on FHIR (estimate below). During a Gemini outage, Groq could serve about 2 questions a minute and roughly 40-60 a day before its daily token cap. Enough for a demo; if Groq is also out, the answer says provider unavailable, as today.
  - Plan (one PR): (1) `ACTIVE_PROVIDER` default `cerebras` becomes `gemini`; (2) a fallback wrapper in `nl_to_sql_planner.py` retries a provider-unavailable call once on Groq, and `CallMetrics.provider` and the trace record who served it; `PLANNER_FALLBACK` (default `groq`, `none` turns it off); (3) Render gets `GROQ_API_KEY` as a dashboard secret (owner) and `PLANNER_FALLBACK=groq` in `render.yaml`; (4) unit tests: a 429 from Gemini is served by Groq, a 400 is not retried, and both down gives provider-unavailable; (5) README step.
  - Today: mostly built. `PLANNER_PROVIDER` already switches between gemini, cerebras, groq, xai and anthropic without code changes. If it is unset, it falls back to cerebras rather than the gemini that CI uses, and there is no automatic fallback when a provider is down.
  - **Done 2026-11-09 (overnight run), with item 14, in the PR "Items 8 + 14".** Unset `PLANNER_PROVIDER` is `gemini`; a provider-unavailable call (the eval harness's own classifier, now `nl_to_sql_planner.is_provider_unavailable`) is retried once on `PLANNER_FALLBACK` (default `groq`); `planner_metrics` and the plan/critic spans carry `provider` (who served it) and `fallback_from`; the eval harness forces `PLANNER_FALLBACK=none`. Unit tests cover 429/402/5xx falling back, 400/401/refusal not retried, both down, `none`, a missing Groq key and the trace. **`GROQ_API_KEY` is not set yet** (owner adds it in Render): until then the fallback is off on the deploy and logs that once at startup, so the live fallback is unproven.

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
  - **Status 2026-11-09: built, in the PR "Item 11", not yet run live.** `eval-fhir-manual.yml` checks out `main` beside the branch, runs the eval on both against the same Gold, and decides with `scripts/compare_evals.py`:
    - it passes only if the branch's accuracy over completed runs is at least `main`'s;
    - an inconclusive run on either side is not a pass;
    - per-case scores go side by side in the job summary.
    - It always runs main's copy of the script, never the branch's.
    - The cost is two sweeps of the Gemini daily quota. `compare_main=false` gives one sweep, and `require_all_cases=true` keeps the old bar as an option.
    - Unit-tested on reports. The first live run waits for a day with quota to spare. Unticked until then.

- [ ] **12. Send the deployed app's traces to a free hosted backend** (owner, 2026-11-06), instead of running Phoenix on Render. Phoenix Cloud's free tier, falling back to Langfuse Cloud Hobby if Phoenix Cloud doesn't fit. Keep the SQLite traces as they are.
  - Done when: with the owner's two Render environment variables set, a question asked on the deploy appears in the hosted backend with its steps; the SQLite traces and Traces view are unchanged; a test shows a down, refusing or hanging backend never breaks or slows a request; what can leave the server is documented and pinned.
  - Status (2026-11-07): code, tests and setup steps in PR #55 (US region). **Phoenix Cloud no longer appears in Arize's docs** (self-hosting only; the hosted product is Arize AX, free tier 25k spans / 15 days), so Langfuse Cloud Hobby (50k units / month, 30 days, no card) is used. Owner's decision 2026-11-07: the hosted export drops the approval reason and reviewer/escalation identities and masks SQL literals; the question was to go "after the existing PHI redaction", but no free-text PHI redactor exists, so it is withheld (hash only) until the owner decides whether to build one. Remaining: the owner adds `TRACE_OTLP_ENDPOINT` and `TRACE_OTLP_HEADERS` in Render, then one live trace is checked.

- [x] **13. Push gate on replayed answers, plus a small live Groq check; nightly stays on Gemini** (owner, 2026-11-07; **rewritten by the owner 2026-11-08**). Pushes and the nightly share one Gemini free daily quota; on 2026-11-06 the nightly found it spent and ended inconclusive.
  - **Owner's decision (2026-11-08), replacing the Groq-only plan below:** running the full push gate live on Groq is a poor fit (5-8 pushes a day, ~6 minutes each, and one Groq organisation's 200K tokens/day shared with the Render fallback, which matters more). Instead:
    - the push gate's hospital and FHIR subsets **replay committed model answers** (`PLANNER_PROVIDER=replay`, as the UI E2E does): zero tokens, seconds, and still catches guard, schema-note, harness and graph changes;
    - a **small live check of about 3 cases on Groq** runs on push (`PLANNER_MIN_INTERVAL_SEC=20`), so the fallback model is still exercised;
    - a change to a prompt, a schema note or anything else a model sees **requires re-recording** the affected cases in the same PR; a test fails when a recording no longer matches what the planner would send (fingerprint the request);
    - the nightly and the manual FHIR eval stay live on Gemini and remain the accuracy measure;
    - **no second Groq account** to get more quota.
  - Done when: a push runs the replayed subsets and the live 3-case Groq check; a deliberately changed prompt without re-recording fails the push; the README says which gate tests what; `GROQ_API_KEY` is a repository secret (owner).
  - **Status 2026-11-09 (overnight run): built, in the PR "Item 13".** Both subsets replay `tests/evals/recordings/push_gate.json` (19 recordings, **authored from each case's reference SQL**, like the UI E2E's, because recording a real model needs a live sweep and the owner allowed one only, for #40; `python scripts/record_push_gate.py --live groq` replaces them with a real model's answers once `GROQ_API_KEY` exists). A replay miss is its own failure mode and fails the gate outright, shown end to end by `test_a_missing_recording_turns_the_real_gate_red`; `test_every_subset_case_has_a_recording_for_todays_prompt` fails in pytest when a prompt changes without re-recording. **Seen live on this PR's first push (run 42559758272):** hospital replay 6/6, FHIR replay 13/13 (cached Gold), and the live Groq check 3/3 on `openai/gpt-oss-120b` in 41 s (`t1_count_all` 748, `t2_avg_mspb_pa` 0.984, `t4_best_value_top10` 10 rows) -- so `GROQ_API_KEY` is already a repository secret. **2026-11-09, owner's decision:** `t2_avg_mspb_pa` failed the live check on #69 and #73 the same way: Groq appended `ORDER BY avg_mspb_score, facility_id LIMIT 1` to a one-row average, which DuckDB refuses. It was replaced by `t2_count_ny_5star` (a filtered count, nothing to order) and stays in the bank and the Gemini evals. The difference is noted in the README.
  - The analysis below (2026-11-07) is kept as the record of why.
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

- [x] **14. Remove Cerebras** (owner, 2026-11-07: no longer free on the owner's account). Every place found on 2026-11-07 (`git grep -i cerebras` on `main`; no workflow and not `render.yaml` use it):
  - `nl_to_sql_planner.py`: the module docstring (line 13, and `setx CEREBRAS_API_KEY` at line 20); the `"cerebras"` entry in `PROVIDERS` (lines 51-54); **`ACTIVE_PROVIDER` defaults to `"cerebras"` when `PLANNER_PROVIDER` is unset (line 110)**; the comment on Cerebras's tier change (line 113); `PROVIDER_RATES["cerebras"]` (line 121).
  - `eval_harness.py:344`: the `--provider` help text names cerebras.
  - `tests/integration/test_traces_api.py:35`: clears `CEREBRAS_API_KEY` in a fixture.
  - `.env.example`: lines 17 and 19 (provider list, and "unset falls back to cerebras") and `CEREBRAS_API_KEY=` (line 31).
  - `README.md`: line 36 (a sample response with `"provider": "cerebras"`, `gpt-oss-120b`), line 63 (provider list), line 619 (`gemini | groq | cerebras | xai | anthropic`).
  - `CLAUDE.md`: line 15 (provider list, and "if unset ... falls back to cerebras"), line 56 (`--provider {gemini,cerebras,...}`), line 58 ("groq/cerebras/xai/anthropic remain selectable").
  - `docs/BACKLOG.md`: items 8 and 13 (rewritten above).
  - Also stale, not Cerebras: `eval-on-push.yml:49` says the 5 s pacing is for "Groq free tier ~8K TPM", but the gate runs on Gemini.
  - **Approved by the owner 2026-11-08: done inside item 8's PR, not separately.**
  - Plan: drop the provider entry and its rate, so `PLANNER_PROVIDER=cerebras` fails at startup instead of calling a paid account (today an unknown provider is a bare `KeyError` at `nl_to_sql_planner.py:434`; the PR gives it a clear message); change the default to `gemini` (item 8, same line, so do that line in item 8's PR); fix docs, the README sample (re-run it, or label it as an old example), `.env.example` and CLAUDE.md; a test that `cerebras` is no longer accepted. Order: either before item 8 or folded into it, the owner's call.

## Production architecture (owner, 2026-11-08)

What real AI, data and software engineering teams would add, checked against the code on
2026-11-08 so nothing is built twice. Work them in the "Order of work" above.

- [ ] **15. Code quality gates.** `ruff` (lint and format), `mypy` (gradual: strict on
  `sql_guard.py`, `phi_guard.py` and `critic.py` first, then widened), and a coverage report
  with a floor at today's level, in `tests.yml`; a `pre-commit` config with the same tools.
  - Done when: a lint error, a type error in a strict module, or coverage below the floor
    fails the `pytest` workflow; the README's CI section lists them.
  - Today: no linter, type checker or coverage in CI (`tests.yml` runs pytest and the
    guard's editable install only). 766 tests.
  - **Status 2026-11-09 (overnight run): built, in the PR "Item 15", except one part.** In `tests.yml`: `ruff check` (floor: E9/F63/F7/F82, zero hits today; F401/F811/F841 have 45 hits, many in `src/`, next), `ruff format --check` with a ratchet list of the 139 files that predate it (a file leaves the list in its own PR), `mypy` strict on `critic.py` and `nightly_alerts.py`, coverage `--cov-fail-under=67` (68.8% measured, tests/demos/scripts not counted); `.pre-commit-config.yaml` with the same tools. **Not done: strict mypy on `sql_guard.py` (8 errors) and `phi_guard.py` (62)** -- fixing them edits the guard files, which the owner merges; they are the next PRs under this item. Unticked until then.

- [ ] **16. Supply chain and merge rules.** Dependabot for pip and GitHub Actions (grouped,
  weekly); CodeQL for Python and the workflows; `pip-audit` on both requirements files in
  CI; branch protection on `main`, which the **owner turns on** in GitHub settings, requiring
  `pytest`, `ui-e2e` and `mcp-e2e` (not the eval gates, which warn when the provider is out).
  - Done when: the first Dependabot PR appears; a known-vulnerable pin fails `pip-audit`;
    `gh api repos/erjonb19/governed-clinical-agent/branches/main --jq .protected` is `true`.
  - Today: none of these; `main` is unprotected.
  - **Status 2026-11-09 (overnight run): built, in the PR "Item 16", except two owner steps.** `.github/dependabot.yml` (pip + actions, grouped, weekly), `codeql.yml` (python + actions; push/PR to main, weekly), a `pip-audit` job in `tests.yml` (pinned 2.10.1, both requirements files). **It found real advisories:** `mcp==1.28.0` had 6 (fixed: bumped to 1.30.0 in this PR; the MCP unit tests and the MCP E2E pass on it) and `oauthlib` 3.3.1 (transitive, from `databricks-sql-connector`) has 2, fixed in 4.0.1, which needs `databricks-sql-connector` 4.4.0 -> 4.9.0. That bump is in its own PR for the owner: it changes the Databricks path, which cannot be re-verified against a workspace until item 10's secrets exist. Until then `pip-audit` ignores those two IDs **by name** (a new advisory still fails). Shown failing: with `mcp==1.28.0` pip-audit exits non-zero (9 found).
  - **Owner steps:** (1) decide on the connector PR; (2) turn on branch protection: Settings -> Branches -> Add branch ruleset (or "Add rule") for `main` -> require a pull request, and require status checks `pytest`, `ui-e2e` and `mcp-e2e` (not the eval gates) -> Save. Then `gh api repos/erjonb19/governed-clinical-agent/branches/main --jq .protected` prints `true`. Unticked until both, and the first Dependabot PR, exist.

- [ ] **17. A container image.** A `Dockerfile` for the API (slim base, non-root user,
  pinned requirements, a `HEALTHCHECK` on `/health`) and a `docker-compose.yml` that starts
  the API and Phoenix together for one-command local runs; the image is built and scanned
  (Trivy, failing on fixable critical findings) in CI; item 5's release workflow pushes it to
  GHCR **(ask)**. Whether Render switches from its Python runtime to the image is the
  owner's call **(ask)**.
  - Done when: `docker compose up` gives a working API with traces in Phoenix; the scan runs
    on every push; the image's memory at idle is measured (Render budget).
  - Today: no Dockerfile for the API (`docker/Dockerfile.sandbox` is the Milestone-1 tool
    sandbox); Render builds from `requirements-api.txt`.
  - **Status 2026-11-09 (overnight run): built, in the PR "Item 17".** `Dockerfile` (python:3.11-slim, `requirements-api.txt`, non-root `app` user owning only `logs/`, `HEALTHCHECK` on `/health`, `GIT_COMMIT` build arg reported by `/health`), `.dockerignore`, `docker-compose.yml` (API + `arizephoenix/phoenix:version-21.0.0`), `container.yml` (build, start, wait healthy, non-root check, smoke test of `/v1/health`, auth and the guard, memory, and a Trivy CRITICAL-fixable gate run from `aquasec/trivy` pinned by digest, 0.58.2, not the movable `trivy-action` tag). **Measured locally:** healthy, 109 MiB idle, 118 MiB after 30 guarded queries, 743 MB image -- inside the Render budget. **Not yet verified:** `docker compose up` end to end with Phoenix (this sandbox's Docker daemon had no route to Docker Hub for the Phoenix pull) and the first Trivy result (CI). **Owner (ask):** pushing the image to GHCR, and whether Render switches to it. Unticked until compose and the scan are seen working. **2026-11-09, owner's decision:** push the v1.1.0 tag and the image to GHCR. `release.yml` gains an `image` job: on a release tag it pushes `ghcr.io/erjonb19/governed-clinical-agent:X.Y.Z` and `:latest` with the job's own token (`packages: write`), pulls the image back, starts it, and writes its idle memory to the job summary. Render stays on the Python runtime unless the image idles under 400 MB.

- [ ] **18. A package layout** (plan first, then the owner's OK). About 60 modules sit at the
  repo root beside `src/` (the Milestone-1 runtime). Move them into one package with
  layers (`api`, `agent`, `guard`, `data`, `evals`, `observability`), one typed settings
  object (pydantic-settings) instead of scattered `os.environ` reads, and an app factory.
  - Done when: the owner has approved a written plan, and then, in small PRs, each layer
    moves with no behavior change (all tests, both evals on replay, the E2Es green); the
    guard modules move last and alone; old import paths keep working until the last PR.
  - **Status 2026-11-09 (overnight run): plan written, waiting for the owner's OK** -- `docs/plans/package-layout.md` (target layers, one typed settings object, an app factory, order of PRs with shims, what must survive, four decisions). Nothing moved.
  - Today: flat layout; `CLAUDE.md` rule 6 (schema and planner agree) and the guard's pinned
    install must survive the move.

- [ ] **19. Durable state in Postgres.** Approvals, `approval_events`, LangGraph checkpoints
  (`PostgresSaver`), traces, the audit log, usage and item 6's cache move behind one
  `STATE_DB_URL` setting: Postgres when it is set, SQLite (today's behavior) when not, so
  local runs and tests are unchanged. The hosted database is a free tier **(ask: the owner
  creates the account)**; compare Neon and Supabase from their own pages first (limits,
  pausing, data retention) and write the choice under this item.
  - Done when: an approval made on the deploy survives a Render redeploy; the integration
    tests run against both SQLite and a Postgres service container in CI.
  - Today: all state is SQLite on Render's disk, which a restart or redeploy can wipe.
  - **Status 2026-11-09 (overnight run): first part built, in the PR "Item 19 (part 1)".** `state_db.py`: `STATE_DB_URL=postgresql://...` puts the approval store (`approvals`, `approval_events`) and both graphs' LangGraph checkpointers (`PostgresSaver`) in Postgres; unset, everything is the SQLite it was. A thin adapter keeps the stores' SQL (`?` -> `%s`, dict rows, dialect-aware DDL and column listing). `/health` says `postgres` or `sqlite` (no host, no credentials). Checkpoints use a `psycopg_pool` pool that re-checks connections (free hosted plans drop idle ones). **Tests never use `STATE_DB_URL`** (the app loads `.env` on import, and the suite drops tables): only `TEST_STATE_DB_URL` on a database whose name contains `test` -- shown with a second database named in `STATE_DB_URL` left with 0 tables. CI: a `pytest-postgres` job runs the **whole suite** against a Postgres 16 service container (1208 passed locally against Postgres 16; 1205 on SQLite), and `tests/integration/test_state_postgres.py` shows an approval and its decision chain surviving a "redeploy" (a new store on an empty disk). Tests that prove no rows reach a checkpoint now read Postgres too (`tests/state_helpers.py`), so they cannot pass vacuously. Pinned: `psycopg[binary]==3.3.8`, `langgraph-checkpoint-postgres==3.2.0`.
  - **Not yet:** traces, the answer cache, the audit log and usage are still SQLite (part 2). **Owner (ask):** pick the free hosted Postgres (compare Neon and Supabase from their own pages: limits, pausing, retention) and create it; then `STATE_DB_URL` in Render. No account was created. Unticked until an approval survives a real Render redeploy.

- [ ] **20. Concurrency and a load test.** Today every query runs under one global lock
  (`app.py`, `_LOCK`), so the API serves one question at a time. Replace it with read-only
  DuckDB connections per request (or a small pool), bound concurrent LLM calls with a
  semaphore, and return 429 with `Retry-After` when the queue is full. A load test (Locust or
  k6) on replayed answers at 1, 5 and 20 concurrent users, before and after, as a
  dispatch-only workflow job; `docs/benchmarks.md` with p50, p95, p99, throughput, the
  machine, and the bottleneck found and fixed.
  - Done when: the benchmark shows the before and after numbers; the guard and PHI tests
    still pass under concurrency (a test runs denied and allowed queries in parallel and
    checks every decision and audit line).
  - Today: single instance, serialized under a lock (README, "Scope and honest
    limitations"); no load test.
  - **Status 2026-11-09 (overnight run): the measuring half, in the PR "Item 20 (part 1)".** `scripts/load_test.py` (the real API on replayed answers, 1/5/20 users, p50/p95/p99/throughput, $0) and `load-test.yml` (dispatch only). **Baseline, before:** throughput flat at ~40 req/s at every concurrency and latency growing with users (p95 30 ms at 1 user, 145 ms at 5, 534 ms at 20) -- the shape of a queue; DuckDB execution is serialised under `_LOCK`, plan/critic calls are not, and whether the lock is the only cap is what part 2 will show (`docs/benchmarks.md`). **Not done (part 2): replacing the lock** with per-request read-only DuckDB cursors, a model-call semaphore with 429 + `Retry-After`, and the parallel decisions-and-audit test. Left for a reviewed daytime PR: it reworks how the query tool shares its connections and the per-statement timeout, the path every guarded query takes. Unticked.
  - **Status 2026-11-09 (owner: open the PR, the owner merges): part 2 built, in the PR "Item 20 (part 2)".** The global `_LOCK` is gone. The query tool opens a DuckDB cursor of its own per execution (read-only like its connection; the timeout interrupts that cursor only), so `/query`, `/raw-sql` and `/mcp` run side by side. `/propose` and approval decisions serialise among themselves (`_APPROVAL_LOCK`) and no longer block queries. Runs that call the model are capped at `MAX_CONCURRENT_RUNS` (default 4); a caller that waits more than `RUN_QUEUE_WAIT_SEC` (15 s) gets 429 with `Retry-After: 5`. `tests/integration/test_concurrency.py` covers three things: 48 allowed and denied queries on 8 threads, each with its own decision and exactly one policy and one execution audit line with the right outcome; a timed-out query stopping alone while a fast one answers; and the 429. **Measured (`docs/benchmarks.md`): the lock was NOT the cap**, since removing it alone changed nothing. A profile found the tracer was the cap: a new SQLite connection and a full-sync commit per span, under one lock, about 18 ms a run. One long-lived WAL writer took p50 from 26 to 16 ms (1 user) and throughput from ~40 to ~55 req/s. What remains is Python CPU in one process, and more throughput needs more processes (item 21).

- [ ] **21. Two replicas.** With state in Postgres (19) and no global lock (20), run two API
  replicas behind a small load balancer in `docker-compose.yml`, with the UI and MCP E2Es
  passing against it; a short section in `docs/benchmarks.md` on what changes at 10x and
  100x (shared data backend: Databricks SQL, or a read-only DuckDB file per replica).
  - Done when: the E2Es pass with two replicas and a request routed to either one sees the
    same approvals and traces.
  - Today: single instance.

- [ ] **22. Users, roles and per-user keys.** Today one shared `API_KEY`. Per-user keys,
  stored hashed (in 19's database), with roles: viewer (Overview), analyst (asks questions),
  reviewer (approves and rejects), admin (keys and schedules). The reviewer's identity comes
  from the key, not from a typed name; per-key quotas and cost budgets (from
  `cost_tracker.py`); rotation and revocation from an admin command; every audit line
  records the user. OAuth 2.1 for MCP stays under "Later" but uses the same identities.
  - Done when: a test per role shows each refused outside its rights; a revoked key is
    refused at once; the audit log names the user for every decision.
  - Today: one key for everything; MCP has its own key (`MCP_API_KEY`).

- [ ] **23. Free-text PHI redaction.** Item 12 withholds the visitor's question from hosted
  traces because no free-text redactor exists. Build one for questions and free text
  (Microsoft Presidio, open source, plus rules for dates, ages over 89, record numbers and
  phone numbers), used before anything leaves the server (hosted traces, logs), and to warn
  when someone types PHI into the public demo. Whether the hosted export then sends the
  redacted question is the owner's call **(ask)**.
  - Done when: tests with planted identifiers show every one removed; its memory is measured
    (Render budget; a regex-only mode if the full model does not fit).
  - Today: `src/utils/redaction.py` masks secrets (keys, tokens), not PHI.

- [ ] **24. The feedback loop.** Thumbs up or down, with a reason, on each answer in the UI;
  stored per answer with its trace id; a script turns a bad answer into a candidate eval
  case for the owner to finish (as the data intake platform's S11.5 does).
  - Done when: a thumbs-down in the UI produces a candidate case file and its trace link;
    `/metrics` shows the share of answers rated down.
  - Today: no user feedback is collected.

- [ ] **25. RAG over CMS measure definitions** (the README's "Next"). Retrieval over CMS's
  public measure documentation, so the agent can answer what a measure means, with
  citations and the existing groundedness check (`groundedness.py`); eval cases for
  definition questions. A small local vector index (DuckDB's vector extension or
  sqlite-vec) and free embeddings; memory measured (Render budget).
  - Done when: definition questions answer with citations and pass their eval cases.
  - Today: not built.

- [ ] **26. Read the data intake platform's gold layer** **(ask: touches the other repo)**.
  After that repo's Stage 22: a versioned, de-identified healthcare export becomes a data
  source here, with a contract between the two repos, its tables on the allowlist and in
  `SCHEMA_DOC`, eval cases, and an E2E check (rule 6 applies).
  - Done when: a file dropped into the intake platform can be asked about here, through the
    guard.

- [ ] **27. Showcase.** `docs/ARCHITECTURE.md`: one page, one diagram, "how it scales and
  where it breaks" with item 20's numbers, and the main trade-offs, each linked to its
  decision; a three-minute walkthrough script for the owner to record (ask, guard refusal,
  approval, trace), linked from the README.
  - Done when: both exist and the README links them.

- [x] **28. Security audit against `docs/SECURITY_STANDARDS.md`** (owner, 2026-11-09). Plan only, change no code. Audit the repo against Section 19.3 of `docs/SECURITY_STANDARDS.md` and every section that applies to this stack, and report each gap in the file's SEVERITY format. Add one backlog item per gap, numbered after this one, with anything Critical (CVSS 9+) first in the Order of work. Propose a `.claude/settings.json` from Section 17.2, tailored so unattended runs don't stall: keep the deny list, and explain each change to the ask list. Check every vendor-specific setting (npm, uv, GitHub, Claude Code permission syntax) against the vendor's current docs before proposing it. One docs-only PR, which the owner reviews and merges himself (security decisions are his). Check Section 14.2 first: the agent's DuckDB connection should have external access off and the configuration locked.
  - **Status 2026-11-10: done, in the PR "Item 28".** `docs/SECURITY_AUDIT_2026-11-10.md`:
    - **Findings:** no Critical, 2 High (14.2 DuckDB external access on and unlocked, confirmed on a live connection; Actions not pinned to SHAs), 21 Medium, 17 Low, each in the SEVERITY format.
    - **Backlog:** one new item per finding, items 30-69 below, with a proposed placement.
    - **`.claude/settings.json`:** a tailored proposal, checked against the current Claude Code docs. Not committed: the owner commits it.

### Security audit items (item 28, 2026-11-10; the owner places them in the Order of work)

- [ ] **30. DuckDB external access on, configuration unlocked** (High; security audit finding 1, `docs/SECURITY_AUDIT_2026-11-10.md`).
  - Where: analytics_query_tool.py:155, :171, :204; data_backends.py:37
  - Done when: open every agent connection with enable_external_access, autoinstall and autoload off and lock_configuration on (lock last); a test that a cursor cannot SET it back or read a file with the guard bypassed; a test or check shows it.
- [ ] **31. No GitHub Action is pinned to a full commit SHA** (High; security audit finding 2, `docs/SECURITY_AUDIT_2026-11-10.md`).
  - Where: every .github/workflows/*.yml, ~52 `uses:` lines (e.g. tests.yml:22 actions/checkout@v7, codeql.yml:29 github/codeql-action/init@v4)
  - Done when: Pin each to `@<40-char sha> # vX.Y.Z`; Dependabot's github-actions updates (already on) keep SHAs current.; a test or check shows it.
- [ ] **32. Reviewer identity is a typed name; anyone can approve anything, including their own proposal** (Medium; security audit finding 3, `docs/SECURITY_AUDIT_2026-11-10.md`).
  - Where: app.py:417 (decided_by: str), app.py:932-966, approval_graph.py:275, web/index.html:1086
  - Done when: Reviewer from the verified key (item 22); until then mark decided_by "self-asserted"; refuse a decision by the proposer's key or by anyone but `assigned_to`; run the groundedness check on edited text.; a test or check shows it.
- [ ] **33. No security headers on any response** (Medium; security audit finding 4, `docs/SECURITY_AUDIT_2026-11-10.md`).
  - Where: app.py:289-335 (middleware), app.py:381-391 (/ui sets only Cache-Control)
  - Done when: A header middleware: CSP `default-src 'self'; script-src 'sha256-<inline>'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'`, nosniff, Referrer-Policy strict-origin-when-cross-origin, Permissions-Policy, HSTS; a test on /ui.; a test or check shows it.
- [ ] **34. No request body or field size limits** (Medium; security audit finding 5, `docs/SECURITY_AUDIT_2026-11-10.md`).
  - Where: app.py:396-423 (question, sql, decided_by, reason, edited_proposal: no max_length)
  - Done when: Field(max_length=...) (question 2,000, sql 20,000, names 200) and a 64 KB body cap.; a test or check shows it.
- [ ] **35. Failed API-key attempts are neither rate-limited nor logged** (Medium; security audit finding 6, `docs/SECURITY_AUDIT_2026-11-10.md`).
  - Where: app.py:171-178 (dependency runs before the slowapi decorator); mcp_server.py:542, :549
  - Done when: Count and audit 401s per client IP in middleware (or move auth after the limiter); same for the MCP branches.; a test or check shows it.
- [ ] **36. /datasets is unauthenticated and unthrottled, and runs one query per table** (Medium; security audit finding 7, `docs/SECURITY_AUDIT_2026-11-10.md`).
  - Where: app.py:601-638
  - Done when: require_api_key + RATE_LIMIT_READ, or cache the counts per data vintage as /findings does.; a test or check shows it.
- [ ] **37. LLM consumption is limited by rate only, per process, with no budget** (Medium; security audit finding 8, `docs/SECURITY_AUDIT_2026-11-10.md`).
  - Where: app.py:145-168 (memory://, 20/min per key); agent_graph.py:87; nl_to_sql_planner.py:88
  - Done when: A global daily request and cost budget checked against the worst case before each call (the scheduled_runs.py pattern), max_length on questions (5), provider-side spend caps.; a test or check shows it.
- [ ] **38. Audit events do not say who asked or from where** (Medium; security audit finding 9, `docs/SECURITY_AUDIT_2026-11-10.md`).
  - Where: src/runtime/audit_logger.py:38-56; app.py:212 (agent_id="api"); render.yaml:16
  - Done when: request_id, principal (hash/prefix of the key) and source_ip on every AuditEvent via middleware; uvicorn --proxy-headers --forwarded-allow-ips.; a test or check shows it.
- [ ] **39. The audit trail is lost on every redeploy, is editable by the app, and has no backup** (Medium; security audit finding 10, `docs/SECURITY_AUDIT_2026-11-10.md`).
  - Where: app.py:154, 211-213 (JSONL under logs/); approval.py:110-145 (SQLite unless STATE_DB_URL)
  - Done when: Ship audit events to an append-only store (an INSERT-only table in the item-19 Postgres, or a log drain); set STATE_DB_URL on the deploy; a dated restore test in docs/runbooks.; a test or check shows it.
- [ ] **40. Raw exception text reaches clients, including unauthenticated /health** (Medium; security audit finding 11, `docs/SECURITY_AUDIT_2026-11-10.md`).
  - Where: app.py:664, :690 (planner error: {e}), :906/:958 (503), :966 (400); /health at app.py:548-573 returns planner_error, graph_error, approval_error and the gold_db path
  - Done when: A fixed message plus trace_id to clients; details in the trace; /health reports exception type only (as mcp_error already does).; a test or check shows it.
- [ ] **41. The free-text question goes to the model providers unredacted** (Medium; security audit finding 12, `docs/SECURITY_AUDIT_2026-11-10.md`).
  - Where: nl_to_sql_planner (via app.py:651 and the graph)
  - Done when: An identifier pass before the planner call (item 23's redactor), log only the decision, and a "do not enter real patient data" notice in the UI.; a test or check shows it.
- [ ] **42. HTTP MCP uses one static shared key, falling back to API_KEY** (Medium; security audit finding 13, `docs/SECURITY_AUDIT_2026-11-10.md`).
  - Where: mcp_server.py:455-458, 539-543
  - Done when: OAuth 2.1 resource-server auth (on the roadmap); until then require a distinct MCP_API_KEY with no fallback, and rotate it.; a test or check shows it.
- [ ] **43. The row cap is not enforced on UNION / set operations** (Medium; security audit finding 14, `docs/SECURITY_AUDIT_2026-11-10.md`).
  - Where: sql_guard.py:293-295; analytics_query_tool.py:108 (fetchall), :323 (slice afterwards)
  - Done when: Wrap a non-SELECT top node as SELECT * FROM (...) LIMIT cap; fetchmany(cap+1).; a test or check shows it.
- [ ] **44. main has no branch protection, and a workflow pushes straight to it** (Medium; security audit finding 15, `docs/SECURITY_AUDIT_2026-11-10.md`).
  - Where: repo settings (protection 404, rulesets []); data-refresh.yml:20-21, :117
  - Done when: A ruleset on main: required checks (pytest, pip-audit, container, CodeQL, ui-e2e, mcp-e2e), no force push or deletion; data-refresh opens a PR instead of pushing. (The owner said he is turning protection on; this records it.); a test or check shows it.
- [ ] **45. No lockfile, no hashes, no release-age cooldown for Python installs** (Medium; security audit finding 16, `docs/SECURITY_AUDIT_2026-11-10.md`).
  - Where: requirements*.txt (== top-level only); render.yaml:15; Dockerfile:23
  - Done when: uv with a committed uv.lock and `exclude-newer = "1 week"` (uv's documented cooldown; needs a recent uv), `uv sync --locked` in CI, Render and the Dockerfile. Or `--generate-hashes` + `--require-hashes`.; a test or check shows it.
- [ ] **46. Dependabot uses only the 3-day default cooldown** (Medium; security audit finding 17, `docs/SECURITY_AUDIT_2026-11-10.md`).
  - Where: .github/dependabot.yml:6-29
  - Done when: `cooldown: { default-days: 7 }` on pip and github-actions (documented for both; security updates are never delayed by it).; a test or check shows it.
- [ ] **47. No secret scanning in pre-commit or CI** (Medium; security audit finding 18, `docs/SECURITY_AUDIT_2026-11-10.md`).
  - Where: .pre-commit-config.yaml:6-24; no gitleaks/trufflehog job
  - Done when: gitleaks pre-commit hook and a CI job (`gitleaks detect --redact`), pinned.; a test or check shows it.
- [ ] **48. Four workflows set no top-level permissions** (Medium; security audit finding 19, `docs/SECURITY_AUDIT_2026-11-10.md`).
  - Where: tests.yml, eval-on-push.yml, mcp-e2e.yml, eval-fhir-manual.yml
  - Done when: `permissions: contents: read` at the top of each.; a test or check shows it.
- [ ] **49. release.yml grants contents:write to the whole workflow, and credentials persist in both write workflows** (Medium; security audit finding 20, `docs/SECURITY_AUDIT_2026-11-10.md`).
  - Where: release.yml:25-26 (pip install … pytest unpinned, tests run under the write token); release.yml:33, data-refresh.yml:28 (checkout without persist-credentials: false)
  - Done when: contents:read at the workflow level, write only on the `gh release create` job; persist-credentials: false except where data-refresh pushes; pin pytest.; a test or check shows it.
- [x] **50. Base images pinned by tag, not digest** (Medium; security audit finding 21, `docs/SECURITY_AUDIT_2026-11-10.md`).
  - Where: Dockerfile:12, docker/Dockerfile.sandbox:1 (python:3.11-slim); docker-compose.yml:36; tests.yml postgres:16
  - Done when: Pin by @sha256 digest; Dependabot `docker` updates keep them current.; a test or check shows it.
  - Done 2026-11-11: `python:3.11-slim` (Dockerfile, docker/Dockerfile.sandbox), the Phoenix image (docker-compose.yml) and the Postgres service (tests.yml) are pinned by the digest each tag pointed to on 2026-11-11 (read from the Docker Hub registry), so builds use the same image as before. Dependabot's new `docker` entry moves the two Dockerfiles; the compose and workflow images are bumped by hand. `tests/unit/test_image_digests.py` fails on any image without a digest.
- [ ] **51. The downloaded FHIR Gold and the Synthea zip are not checksummed** (Medium; security audit finding 22, `docs/SECURITY_AUDIT_2026-11-10.md`).
  - Where: scripts/fetch_fhir_gold.py:93; fetch_synthea.py:56; render.yaml:15 (every deploy)
  - Done when: A committed expected SHA-256 per asset; fail on mismatch.; a test or check shows it.
- [ ] **52. The standalone MCP server writes no audit log** (Medium; security audit finding 23, `docs/SECURITY_AUDIT_2026-11-10.md`).
  - Where: mcp_server.py:131-135, 159-167 (AgentRuntime() without an audit_logger)
  - Done when: Attach an AuditLogger that drops rows in main() / at module start.; a test or check shows it.
- [ ] **53. /docs, /redoc and /openapi.json are public in production** (Low; security audit finding 24, `docs/SECURITY_AUDIT_2026-11-10.md`).
  - Where: app.py:289-294; the / route advertises /docs
  - Done when: docs_url/redoc_url/openapi_url=None unless an env flag is set, or behind the key.; a test or check shows it.
- [ ] **54. Auth is per route instead of deny-by-default; /metrics and /schema are public** (Low; security audit finding 25, `docs/SECURITY_AUDIT_2026-11-10.md`).
  - Where: app.py:595, :849
  - Done when: Require auth on the router and list public routes explicitly (/health, /ui, /ui-config).; a test or check shows it.
- [ ] **55. Request models accept unknown fields; most routes have no response_model** (Low; security audit finding 26, `docs/SECURITY_AUDIT_2026-11-10.md`).
  - Where: app.py:396-423
  - Done when: model_config = ConfigDict(extra="forbid"); response models.; a test or check shows it.
- [ ] **56. One shared plaintext API key, no scopes or revocation** (Low; security audit finding 27, `docs/SECURITY_AUDIT_2026-11-10.md`).
  - Where: app.py:115, :174; mcp_server.py:455-457
  - Done when: covered by item 22 (hashed per-user keys, roles, revocation); tick together; a test or check shows it.
- [ ] **57. /ui-config prefills the API key for any loopback-looking client** (Low; security audit finding 28, `docs/SECURITY_AUDIT_2026-11-10.md`).
  - Where: app.py:567-585
  - Done when: Prefill only with an explicit UI_PREFILL_KEY=1.; a test or check shows it.
- [ ] **58. esc() does not escape the single quote** (Low; security audit finding 29, `docs/SECURITY_AUDIT_2026-11-10.md`).
  - Where: web/index.html:428
  - Done when: Add ' -> &#39;. Not exploitable today (all dynamic attributes are double-quoted).; a test or check shows it.
- [ ] **59. No error tracking or security alerts** (Low; security audit finding 30, `docs/SECURITY_AUDIT_2026-11-10.md`).
  - Where: repository-wide (eval, uptime and SLO alerts exist)
  - Done when: Alert on auth-failure and spend thresholds through a GitHub issue (nightly_alerts pattern); optional free Sentry with send_default_pii=False and a scrubber (new dependency: owner's OK).; a test or check shows it.
- [ ] **60. No retention limit on logs holding free text** (Low; security audit finding 31, `docs/SECURITY_AUDIT_2026-11-10.md`).
  - Where: cost_tracker.py:30-31 (question[:200], forever); audit JSONL (never rotated); approval.py (questions and proposals kept forever)
  - Done when: Store a hash and length in the metrics log; rotate and cap JSONL; a retention period and purge for approvals.; a test or check shows it.
- [ ] **61. The Postgres connection does not require TLS** (Low; security audit finding 32, `docs/SECURITY_AUDIT_2026-11-10.md`).
  - Where: state_db.py:80
  - Done when: sslmode=require (or verify-full) unless the URL sets one.; a test or check shows it.
- [ ] **62. Secret redaction misses this repo's own key formats** (Low; security audit finding 33, `docs/SECURITY_AUDIT_2026-11-10.md`).
  - Where: src/utils/redaction.py:36-37 (sk-, ghp_, Bearer only)
  - Done when: Patterns for AIza (Gemini), gsk_ (Groq), xai- (xAI), sk-ant- (Anthropic), with tests.; a test or check shows it.
- [ ] **63. Audit failures are swallowed silently** (Low; security audit finding 34, `docs/SECURITY_AUDIT_2026-11-10.md`).
  - Where: app.py:727-728 (except: pass), app.py:214-215
  - Done when: Warn on stderr with the exception type; /health shows audit_enabled.; a test or check shows it.
- [ ] **64. Hosted trace export masks single-quoted literals only** (Low; security audit finding 35, `docs/SECURITY_AUDIT_2026-11-10.md`).
  - Where: trace_export.py:134-139; analytics_query_tool.py:53-56
  - Done when: Strip SQL comments before masking; hash or truncate the critic's reason for hosted export. (Export is off by default.); a test or check shows it.
- [ ] **65. Old audit logs are committed although logs/ is gitignored** (Low; security audit finding 36, `docs/SECURITY_AUDIT_2026-11-10.md`).
  - Where: logs/audit/audit_agent_*.jsonl (10 files, demo prompts only)
  - Done when: git rm --cached logs/audit/*.jsonl.; a test or check shows it.
- [ ] **66. SQL comments are allowed and kept in the executed SQL** (Low; security audit finding 37, `docs/SECURITY_AUDIT_2026-11-10.md`).
  - Where: sql_guard.py:186-198 (verified: /* */ and -- pass into safe_sql, which the critic reads)
  - Done when: Deny statements with comments, or emit with comments=False.; a test or check shows it.
- [ ] **67. Retry and critic prompts do not delimit untrusted text** (Low; security audit finding 38, `docs/SECURITY_AUDIT_2026-11-10.md`).
  - Where: agent_graph.py:205-211; nl_to_sql_planner.py:439-466, :613
  - Done when: Labelled delimiters around question, error and SQL; an "untrusted text" rule in the planner's system prompt (as the critic already has). Changes prompts: re-record replays.; a test or check shows it.
- [ ] **68. Adversarial evals are not a CI gate; no file/network-exfiltration case** (Low; security audit finding 39, `docs/SECURITY_AUDIT_2026-11-10.md`).
  - Where: eval_bank_adversarial.py (13 cases), no workflow runs it
  - Done when: A replayed adversarial run in eval-on-push (push_gate pattern); add read_text, ATTACH and URL cases.; a test or check shows it.
- [ ] **69. Smaller supply-chain and setup gaps** (Low; security audit finding 40, `docs/SECURITY_AUDIT_2026-11-10.md`).
  - Where: .gitignore (only `.env`; stray BOM on line 1); tests.yml:78 (pip-audit skips requirements-e2e.txt; dev extras in setup.py are >=); container.yml:46-53 (Trivy 0.58.2 with the Docker socket mounted); repo settings (private vulnerability reporting off, Dependabot security updates off); no workflow linter; mcp_server.py:423-447 (demo tools echo/fetch_url/git_push advertised on the production /mcp, all denied by policy); no single kill switch for the agent endpoints.
  - Done when: .gitignore patterns (.env.*, !.env.example, *.pem, *.key, *.p12, *.tfstate, secrets/); audit every requirements file and pin dev extras; current Trivy on a `docker save` tarball (--input) instead of the socket; turn on private vulnerability reporting and a SECURITY.md pointing to it; Dependabot security updates; zizmor in CI; demo tools behind MCP_DEMO_TOOLS=1; an AGENT_DISABLED=1 flag returning 503 on /query, /propose and /mcp.; a test or check shows it.

## Later (keep, don't start)

- [ ] **MCP server over Streamable HTTP with real auth.**
  - Done when: MCP clients authenticate with OAuth 2.1.
  - Today: the premise is out of date. Streamable HTTP with API-key auth is **already built** (standalone and at `/mcp`, end-to-end tested on every push). What is missing is OAuth 2.1.
- [x] **Databricks lift, so the Databricks backend has actually run.** Verified once on a real Free Edition workspace (6-case eval subset at 100%, backend parity). The remaining gap, keeping it fresh and re-checked, is item 10.
- [ ] **Real users:** the owner will find three to five. Add a README section where their feedback is recorded.
  - Done when: the README has a "User feedback" section with one entry per user (date, role, what they tried, what they said).
