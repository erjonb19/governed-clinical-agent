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
- **Tests:** 570 passed, 1 skipped (`pytest`).
- **Evals:** none needed (no planner change).
- **Anthropic spend so far:** $0.00.
- **You need to:** nothing for this PR.
