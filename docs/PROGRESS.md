# Progress: production readiness

One line per finished item from `docs/BACKLOG.md`, newest last:
**what changed**, and **how to check it**, with the PR link.

| Date | Item | What changed | How to check it | PR |
|---|---|---|---|---|
| 2026-11-06 | 1. CI covers everything | pytest runs on every push to any branch and nightly; every push also runs a 13-case FHIR subset gate after the hospital one; nightly evals retry an inconclusive run once, then go red (never grey) | Actions tab: `tests (pytest)` on any branch push and on the nightly schedule; `eval (quick gate)` shows the `fhir-quick-eval` job; run 42238076116 shows the nightly red with "Eval inconclusive twice" | #53 |
| 2026-11-06 | 2. Tracing | Every step of a run, including each `route` the graph takes, is sent to a self-hosted Arize Phoenix when `TRACE_OTLP_ENDPOINT` is set (`trace_export.py`); Phoenix runs locally, not on Render | README "Opening a trace in Arize Phoenix": start Phoenix, start the API with the endpoint, ask a question, open the newest trace at http://localhost:6006 | see PR |
| 2026-11-09 | 8 + 14. Provider setting, Groq fallback, Cerebras removed | Unset `PLANNER_PROVIDER` means Gemini; when the provider is down (402/429/5xx) the call is retried once on Groq (`PLANNER_FALLBACK`, default `groq`) and the answer and its trace say who served it (`fallback_from`); Cerebras is gone and is refused at startup | `pytest tests/unit/test_planner_fallback.py`; live: once `GROQ_API_KEY` is set in Render, a `/query` answered while Gemini is out shows `"provider": "groq", "fallback_from": "gemini"` in `planner_metrics` | see PR |
| 2026-11-09 | 13. Push gate on replayed answers | The hospital and FHIR push gates replay committed answers (no model call, no Gemini quota); a prompt, schema note, question or reference changed without re-recording fails the push; a live 3-case check runs on Groq; the nightly stays live on Gemini | Actions -> `eval (quick gate)` on any push: `quick-eval` and `fhir-quick-eval` finish in seconds, `groq-live-check` shows 3 Groq cases (run 42559758272); change a schema note without `python scripts/record_push_gate.py` and pytest plus the gate go red | see PR |
