# Progress: production readiness

One line per finished item from `docs/BACKLOG.md`, newest last:
**what changed**, and **how to check it**, with the PR link.

| Date | Item | What changed | How to check it | PR |
|---|---|---|---|---|
| 2026-11-06 | 1. CI covers everything | pytest runs on every push to any branch and nightly; every push also runs a 13-case FHIR subset gate after the hospital one; nightly evals retry an inconclusive run once, then go red (never grey) | Actions tab: `tests (pytest)` on any branch push and on the nightly schedule; `eval (quick gate)` shows the `fhir-quick-eval` job; run 42238076116 shows the nightly red with "Eval inconclusive twice" | #53 |
| 2026-11-06 | 2. Tracing | Every step of a run, including each `route` the graph takes, is sent to a self-hosted Arize Phoenix when `TRACE_OTLP_ENDPOINT` is set (`trace_export.py`); Phoenix runs locally, not on Render | README "Opening a trace in Arize Phoenix": start Phoenix, start the API with the endpoint, ask a question, open the newest trace at http://localhost:6006 | see PR |
| 2026-11-09 | 8 + 14. Provider setting, Groq fallback, Cerebras removed | Unset `PLANNER_PROVIDER` means Gemini; when the provider is down (402/429/5xx) the call is retried once on Groq (`PLANNER_FALLBACK`, default `groq`) and the answer and its trace say who served it (`fallback_from`); Cerebras is gone and is refused at startup | `pytest tests/unit/test_planner_fallback.py`; live: once `GROQ_API_KEY` is set in Render, a `/query` answered while Gemini is out shows `"provider": "groq", "fallback_from": "gemini"` in `planner_metrics` | see PR |
| 2026-11-09 | 4. Version on every answer | Every `/query` answer, its trace and an `answer` audit line say which prompt version, model, provider and git commit produced it | Ask a question in the UI or `POST /query`: the response has `version`; `GET /traces/<trace_id>` shows the same four in `attrs`; `logs/audit_api_*.jsonl` has an `answer` line with them | see PR |
