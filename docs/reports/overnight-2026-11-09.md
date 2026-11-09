# Overnight run, 2026-11-09

Unattended run under the owner's standing OK from 2026-11-09: merge a PR once pytest,
ui-e2e and mcp-e2e are green and a fresh review finds nothing blocking. Never merge #40,
or anything that changes `sql_guard.py`, `phi_guard.py`, `phi_columns.yaml` or what the
guard allows or refuses. No accounts were created, nothing was spent, no tag was pushed,
no image was published, branch protection was not changed and no other repo was touched.
**No live eval sweep was run in this session's own environment.** The one live sweep
allowed (#40's full FHIR eval) is dispatched on a GitHub runner after Gemini's 08:00 UTC
reset. Its result is under "#40" below.

Each code PR below was read by a separate review agent before it merged. Its blocking
findings were fixed in the same PR first: for example, on #72 a test could have wiped a real
`STATE_DB_URL` database, and on #73 the benchmark overstated what the lock explains. None of
these PRs touches the guard files.

## What merged

| PR | Item | What it does |
|---|---|---|
| #58 | backlog | The owner's "Order of work", decisions on 8/13/14, items 15-27 (docs only) |
| #59 | 8 + 14 | Gemini is the default when `PLANNER_PROVIDER` is unset. A provider-unavailable call is retried once on Groq (`PLANNER_FALLBACK`, default `groq`, `none` off). Cerebras is removed everywhere. |
| #60 | 13 | The push gate replays committed answers (`tests/evals/recordings/push_gate.json`), so it tests guard, harness, Gold and scoring, not a model. A stale recording fails the push. A live 3-case Groq check runs beside it. |
| #61 | 3 | Nightly eval alerts as GitHub issues: `eval-regression` and `provider-outage`, opened, commented and closed by `nightly_alerts.py` |
| #62 | 4 | Prompt version, model, provider and git commit on every answer, its trace and an `answer` audit event |
| #63 | 5 | `/v1` paths (the old ones still work), one `VERSION` file (1.1.0), version and commit on `/health`, and `release.yml` (runs on a tag only; the owner pushes tags) |
| #64 | 15 (most) | ruff check and a format ratchet, strict mypy on `critic.py` and `nightly_alerts.py`, a coverage floor of 67% (68.8% measured), pre-commit |
| #65 | 16 (most) | Dependabot, CodeQL, pip-audit. pip-audit found 6 real advisories in `mcp` 1.28.0, fixed by moving to 1.30.0. |
| #67 | 17 (most) | API container image (non-root, HEALTHCHECK), compose with Phoenix, `container.yml` (build, smoke test, Trivy pinned by digest, 0 CRITICAL) |
| #68 | 18 | The package-layout **plan only** (`docs/plans/package-layout.md`). It waits for the owner's OK. |
| #69 | 6 | Answer cache: repeated questions skip the model. Only the model's text is cached, never rows, and the guard, query and output check still run on a hit. `/metrics` shows the hit rate. |
| #72 | 19 (part 1) | `STATE_DB_URL=postgresql://...` puts approvals and both graphs' checkpoints in Postgres. CI runs the whole suite against a Postgres 16 service container. No hosted account was created. |
| #73 | 20 (part 1) | Load test on recorded answers and the baseline in `docs/benchmarks.md` |

## Still open, and why

- **#40 (age cap)** touches the guard, so it is the owner's to merge. It is up to date with
  `main` and its push-gate recordings were re-recorded. Its full FHIR eval is dispatched on
  a GitHub runner after the 08:00 UTC reset; the result is added below once it finishes.
- **#46 (de-identified FHIR layer, PR A)** was left as it was, per the Order of work.
- **#66 (`databricks-sql-connector` 4.4.0 -> 4.9.0)** fixes the two `oauthlib` advisories.
  It changes the Databricks path, which can't be re-verified until item 10's secrets exist.
  Until then pip-audit ignores exactly those two IDs, by name.
- **#70 and #71 (Dependabot, actions and python groups)** came from the first Dependabot
  run. They are the owner's call: they bump many packages at once, and none fixes a known
  advisory.

## Items not finished, and what they wait on

| Item | Built | Waits on |
|---|---|---|
| 15 | everything except strict mypy on the guard | Strict mypy on `sql_guard.py` (8 errors) and `phi_guard.py` (62). Fixing them edits guard files, so those PRs are the owner's to merge. |
| 16 | everything except two steps | **Owner:** branch protection on `main` (required checks are listed in BACKLOG item 16), and #66 |
| 17 | image, compose, CI scan | **Owner (ask):** push the image to GHCR, and decide whether Render runs it. `docker compose up` with Phoenix was not run end to end, because this sandbox could not pull from Docker Hub. |
| 18 | plan | **Owner:** OK the plan before any code moves |
| 19 | Postgres path, tested in CI | **Owner (ask):** a free hosted Postgres account and `STATE_DB_URL` in the Render dashboard |
| 20 | load test + baseline | Part 2, replacing the global `_LOCK` with per-request read-only cursors and a model-call semaphore. That change reworks how every guarded query shares its connection and timeout, so it was left for a reviewed daytime PR. |
| 13 / 8 | built | **Owner:** set `GROQ_API_KEY` in the Render dashboard, so the deployed app can fall back. The repo secret appears to be set: the live Groq check ran instead of skipping. |
| 12 | built earlier | **Owner:** the Langfuse variables in the Render dashboard |
| 10 | — | **Owner:** Databricks secrets |

**Problem found: the live Groq check fails on the same case.** It runs 3 cases once each.
On #69 and again on #73 it failed `t2_avg_mspb_pa` in the same way: Groq
(`openai/gpt-oss-120b`) appended `ORDER BY avg_mspb_score, facility_id LIMIT 1` to a
one-row average. DuckDB rejects that (`Binder Error`), and the harness scores it
`guard_denied`. Neither PR touched the planner. This looks like a repeatable weakness of
that model on that question, not random noise, and not a code regression: the replayed
gates and Gemini are unaffected. The owner chooses one of three options: swap
`t2_avg_mspb_pa` out of the live check for another case, run each case 3 times with the
80% gate, or report the check as a warning. I did not change it: picking the cases a check
runs is the owner's call, and editing a case to make it pass is ruled out.

## How to check each item

**Merged work (any machine with the repo):**
1. `git pull` on `main`.
2. `pip install -e ".[dev]" && pip install -r requirements-api.txt`
3. `pytest`: the whole suite should pass.
4. On GitHub, open **Actions** and check that the latest `main` run is green for `tests`,
   `eval-on-push`, `e2e`, `mcp-e2e`, `container` and `codeql`.

**Item 8 (fallback):**
1. `pytest tests/unit/test_planner_fallback.py -q`
2. On Render, set `GROQ_API_KEY`. After the next deploy, `/health` keeps working. When
   Gemini returns a 429, the trace of that run shows `fallback_from: gemini`.

**Item 13 (push gate):**
1. Open any recent push's `eval-on-push` run. `quick-eval` and `fhir-quick-eval` say
   `replay` and pass. `groq-live-check` ran live.
2. To see a stale recording fail: change one word in a prompt in `nl_to_sql_planner.py`
   and run `pytest tests/unit/test_push_gate_recordings.py -q`. It fails.

**Item 3 (alerts):** `pytest tests/unit/test_nightly_alerts.py -q`. After the next nightly
run, a red job opens an `eval-regression` or `provider-outage` issue.

**Items 4 + 5 (version, /v1):**
1. `uvicorn app:app`, then open `http://localhost:8000/v1/health`. It shows `version`,
   `api_version` and `git_commit`.
2. Ask a question in `/ui`. The JSON answer has a `version` block.

**Item 6 (cache):** ask the same question twice in `/ui`, then open `/metrics`.
`llm_cache.hits` is 1.

**Item 17 (container):**
1. `docker build -t gca .`
2. `docker run -p 8000:8000 -e API_KEY=x gca`
3. Open `http://localhost:8000/v1/health`.

**Item 19 (Postgres):** with Docker, run
`docker run -d -p 5432:5432 -e POSTGRES_PASSWORD=pw -e POSTGRES_DB=state_test postgres:16`,
then `TEST_STATE_DB_URL=postgresql://postgres:pw@localhost:5432/state_test pytest -q`.

**Item 20 (load test):** `python scripts/load_test.py`. It prints p50, p95, p99 and
throughput at 1, 5 and 20 users. Compare them with `docs/benchmarks.md`.

**Item 18 (plan):** read `docs/plans/package-layout.md`, then reply OK or with changes.

## #40: full FHIR eval on a GitHub runner

Run [42585447542](https://github.com/erjonb19/governed-clinical-agent/actions/runs/42585447542),
dispatched at 08:22 UTC after the Gemini reset, on `feat/age-cap` at 0278fc9 (main merged in).
This was the night's only live sweep.

**Result: the job is red (the manual eval requires every case to pass).**
- **#40:** 27/28 cases (majority-correct), 78/84 runs = 92.9%. The 80% gate passes.
- **Compare:** main's newest conclusive runs were 27/28 cases, 82/84 = 97.6% (#50's run
  42462655391), and 27/28, 96.3% (the 2026-11-05 nightly).
- **What #40 is for works:** `f1_avg_age` 3/3 = 48.1 (as expected), and
  `f3_patients_90_plus` 3/3 = 53.
- **All 6 failed runs are `guard_denied`, the guard refusing bad SQL.** None is a wrong answer
  served:
  - `f1_count_active_conditions` 0/3, the case that fails the job. Gemini invented column
    names instead of `is_active`: `is_voiced`, and once
    `is_experiment_or_something_false_no_wait_is_active`. The guard refused each one with
    "no such column", as it should.
  - `f5_elderly_active_conditions` 2/3. One run used `c.is_pressed`, the same pattern on the
    same column.
  - `f4_chemoradiation_and_cisplatin` 2/3. One run wrote its reasoning into the SQL, which
    could not be parsed.
  - `f5_above_avg_bmi_patients` 2/3. One run hit a DuckDB internal error.
- **Different from main:** on #50's run the one failing case was `f4_avg_glucose_prediabetic`,
  and `f1_count_active_conditions` passed 3/3.

**What I can and cannot say.** The garbled `is_active` names appeared only on this branch.
#40 changes the prompt by 3 lines (the "Ages over 90 are counted as 90" note in
`nl_to_sql_planner.py`). So the prompt change may have unsettled the model on that column.
One sweep cannot tell that apart from day-to-day noise in Gemini Flash Lite, and the owner
allowed only one live sweep, so I did not re-run it.

**Owner's decision.** #40 is not merged. A cheap way to separate the two, about 6 model calls
on the free tier: run
`EVAL_DATASET=fhir EVAL_CASES=f1_count_active_conditions,f5_elderly_active_conditions python eval_harness.py`
once on `main` and once on `feat/age-cap`. If main passes and the branch keeps inventing
`is_*` columns, the prompt note needs rewording, and that rewording goes in #40.
