# Progress: production readiness

One line per finished item from `docs/BACKLOG.md`, newest last:
**what changed**, and **how to check it**, with the PR link.

| Date | Item | What changed | How to check it | PR |
|---|---|---|---|---|
| 2026-11-06 | 2. Tracing | Every step of a run, including each `route` the graph takes, is sent to a self-hosted Arize Phoenix when `TRACE_OTLP_ENDPOINT` is set (`trace_export.py`); Phoenix runs locally, not on Render | README "Opening a trace in Arize Phoenix": start Phoenix, start the API with the endpoint, ask a question, open the newest trace at http://localhost:6006 | see PR |
