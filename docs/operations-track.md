# Operations track: how the Oct 25 "Phase 1" maps onto PLAN v2 (2026-10-29)

The Oct 25 four-phase plan named a Phase 1 for this repo: tracing, checkpointing, and
unattended scheduled runs. `docs/PLAN_v2.md` (2026-10-17, on PR #26) already covers most
of it in more detail, so PLAN v2 is the plan and this note only records what Phase 1 adds
to it. (This "Phase 1" is unrelated to Phase 1 in `docs/plan.md`, the
core runtime and policy engine.) Nothing here changes PLAN v2's decisions D1 to D9 or its PR order.

| Phase 1 item | Where it lives in PLAN v2 | What is added |
|---|---|---|
| Tracing | §2, PR 3: `tracing.py`, SQLite spans, `GET /traces`, Traces view | Nothing. Built as specified |
| Checkpointing | Not in v2. `agent_graph` compiles with no checkpointer; only `approval_graph` has `SqliteSaver` | **Folded into PR 3.** `agent_graph` compiles with `SqliteSaver` on `CHECKPOINT_DB`, `thread_id = trace_id`, so one id names the checkpoint thread, the trace and the response. `GovernedAgentGraph.run()` takes an optional `trace_id` to resume a run that failed part-way from its last checkpoint. `GET /traces/{id}` includes the checkpoint summary (`next`, attempts, last_error) for approval traces; planner-graph runs get the same once D2 puts the API on the graph. D2 (`/query` onto the graph) then puts every Ask run on a checkpointed thread |
| Unattended scheduled runs | §7c (nightly Haiku trajectory evals with a budget stop) and §7d (nightly live check of the Render deploy, issue on failure) | **A later item, 7e, after 7c:** a `schedules.yaml` of standing questions run weekly through the graph on Gemini free tier, result hashes compared with the previous run, `drift` or `failed` opening an issue the way 7d does. The questions are ordinary eval cases first, so the schedule never runs a question the suite does not already pass. **Moved ahead of 7c on 2026-10-30** (owner's decision): the job runs on Gemini, so Haiku's accuracy does not gate it. The eval-cases-first rule holds: the four cases ship in the same PR, and `scheduled_runs.py` refuses a schedule whose case is not in the bank. **Built: PR #29** (O3); README section and docs in the O4 PR |

An earlier draft of this note (commit `b0b79a3`) split the same work into items O1 to O4; the table above supersedes it.
