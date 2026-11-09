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

- [ ] **6. Result caching in SQLite**, so repeat questions skip the LLM call. Show the hit rate. Keep in mind the cache resets when Render restarts.
  - Done when: asking the same question twice makes one LLM call, not two; the cache is keyed so a data refresh or a guard change cannot serve a stale answer, and still re-runs the guard; the hit rate shows in `/metrics`; the README says it resets on Render restarts.
  - Today: only the Overview findings are cached (per data vintage). Questions are not.

- [ ] **7. SLOs and P99 latency**, built on the latency tracking that already exists. Write the SLO targets in the README and alert when they're missed.
  - Done when: `/metrics` reports p50, p95 and p99; the README states the SLO targets; a missed SLO opens a GitHub issue (through the item 3 channel).
  - Today: `cost_tracker.py` reports per-call latency and p50/p95. There is no p99 and no SLO.

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
  - **Status 2026-11-09 (overnight run): built, in the PR "Item 13".** Both subsets replay `tests/evals/recordings/push_gate.json` (19 recordings, **authored from each case's reference SQL**, like the UI E2E's, because recording a real model needs a live sweep and the owner allowed one only, for #40; `python scripts/record_push_gate.py --live groq` replaces them with a real model's answers once `GROQ_API_KEY` exists). A replay miss is its own failure mode and fails the gate outright, shown end to end by `test_a_missing_recording_turns_the_real_gate_red`; `test_every_subset_case_has_a_recording_for_todays_prompt` fails in pytest when a prompt changes without re-recording. **Seen live on this PR's first push (run 42559758272):** hospital replay 6/6, FHIR replay 13/13 (cached Gold), and the live Groq check 3/3 on `openai/gpt-oss-120b` in 41 s (`t1_count_all` 748, `t2_avg_mspb_pa` 0.984, `t4_best_value_top10` 10 rows) -- so `GROQ_API_KEY` is already a repository secret.
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

- [ ] **18. A package layout** (plan first, then the owner's OK). About 60 modules sit at the
  repo root beside `src/` (the Milestone-1 runtime). Move them into one package with
  layers (`api`, `agent`, `guard`, `data`, `evals`, `observability`), one typed settings
  object (pydantic-settings) instead of scattered `os.environ` reads, and an app factory.
  - Done when: the owner has approved a written plan, and then, in small PRs, each layer
    moves with no behavior change (all tests, both evals on replay, the E2Es green); the
    guard modules move last and alone; old import paths keep working until the last PR.
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

## Later (keep, don't start)

- [ ] **MCP server over Streamable HTTP with real auth.**
  - Done when: MCP clients authenticate with OAuth 2.1.
  - Today: the premise is out of date. Streamable HTTP with API-key auth is **already built** (standalone and at `/mcp`, end-to-end tested on every push). What is missing is OAuth 2.1.
- [x] **Databricks lift, so the Databricks backend has actually run.** Verified once on a real Free Edition workspace (6-case eval subset at 100%, backend parity). The remaining gap, keeping it fresh and re-checked, is item 10.
- [ ] **Real users:** the owner will find three to five. Add a README section where their feedback is recorded.
  - Done when: the README has a "User feedback" section with one entry per user (date, role, what they tried, what they said).
