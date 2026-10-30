# Operations track: how the Oct 25 "Phase 1" maps onto PLAN v2 (2026-10-29)

The Oct 25 four-phase plan named a Phase 1 for this repo: tracing, checkpointing, and
unattended scheduled runs. `docs/PLAN_v2.md` (2026-10-17, on PR #26) already covers most
of it in more detail, so PLAN v2 is the plan and this note only records what Phase 1 adds
to it. Nothing here changes PLAN v2's decisions D1 to D9 or its PR order.

| Phase 1 item | Where it lives in PLAN v2 | What is added |
|---|---|---|
| Tracing | §2, PR 3: `tracing.py`, SQLite spans, `GET /traces`, Traces view | Nothing. Built as specified |
| Checkpointing | Not in v2. `agent_graph` compiles with no checkpointer; only `approval_graph` has `SqliteSaver` | **Folded into PR 3.** `agent_graph` compiles with `SqliteSaver` on `CHECKPOINT_DB`, `thread_id = trace_id`, so one id names the checkpoint thread, the trace and the response. `GovernedAgentGraph.run()` takes an optional `trace_id` to resume a parked or failed run from its last checkpoint; `GET /traces/{id}` includes the checkpoint summary (node, attempts, last_error). D2 (`/query` onto the graph) then puts every Ask run on a checkpointed thread |
| Unattended scheduled runs | §7c (nightly Haiku trajectory evals with a budget stop) and §7d (nightly live check of the Render deploy, issue on failure) | **A later item, 7e, after 7c:** a `schedules.yaml` of standing questions run weekly through the graph on Gemini free tier, result hashes compared with the previous run, `drift` or `failed` opening an issue the way 7d does. The questions are ordinary eval cases first, so the schedule never runs a question the suite does not already pass. Not built until 7c has measured Haiku's accuracy |

Everything else in the earlier draft of this note (O1 to O4) is superseded by the table above.
