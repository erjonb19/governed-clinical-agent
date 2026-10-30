"""Author the E2E recordings: hand-written responses, replayed exactly like recorded ones.

Two reasons to author rather than record:
  * the guard-denial scenario needs SQL that reads bronze_patient on EVERY
    attempt, which a real model, told the allowlist, declines to write;
  * the other scenarios test the system (UI, API, graph, guard, approvals),
    not a model's SQL, so a reviewed, correct query per question is the
    steadier fixture. Re-recording them from a real model is one command
    (tests/e2e/README.md) and changes nothing else.
Every entry written here is marked "authored": true.

They are authored by running the REAL graph -- the API's runtime, policy, guard
and schema mapping -- with a replay planner in record mode whose source is
scripted. The keys are therefore exactly the requests the server will make,
retry prompts and the guard's denial text included. No network.

    python tests/e2e/author_recordings.py
"""

import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
os.environ.setdefault("ALLOW_OPEN_ACCESS", "true")     # app import, no server started
os.environ["E2E_RECORD"] = "1"
os.environ.pop("REPLAY_SOURCE_PROVIDER", None)

from test_ui_e2e import ASK, DENY, ESCALATE, PROPOSE     # noqa: E402

BRONZE_SQL = "SELECT * FROM bronze_patient LIMIT 10"
SQL = {
    DENY: BRONZE_SQL,
    ASK: "SELECT count(*) AS hospitals FROM gold_hospital_profile WHERE star_rating = 5",
    PROPOSE: ("SELECT facility_id, facility_name, readmit_hf FROM gold_hospital_profile "
              "WHERE readmit_hf IS NOT NULL ORDER BY readmit_hf DESC, facility_id ASC LIMIT 10"),
    ESCALATE: ("SELECT facility_id, facility_name, ed_median_min FROM gold_hospital_profile "
               "WHERE ed_median_min IS NOT NULL ORDER BY ed_median_min DESC, facility_id ASC LIMIT 10"),
}
APPROVE = json.dumps({"verdict": "approve", "reason": "authored E2E fixture", "issues": []})


class Scripted:
    """Stands in for the real provider behind the replay planner."""

    def _complete(self, system, user, role):
        from nl_to_sql_planner import CallMetrics
        q = next(q for q in SQL if q in user)
        text = APPROVE if role == "critic" else SQL[q]
        return text, CallMetrics(latency_ms=0, prompt_tokens=len(system) // 4,
                                 completion_tokens=len(text) // 4, model="authored",
                                 provider="authored")


def main():
    import replay
    from nl_to_sql_planner import NLToSQLPlanner
    import app
    from agent_graph import GovernedAgentGraph
    from analytics_query_tool import AnalyticsQueryTool
    from src.runtime.agent_runtime import AgentRuntime
    import tempfile

    before = set(replay.ReplayStore.from_env().entries)
    planner = NLToSQLPlanner(provider="replay")
    planner._source = Scripted()
    rt = AgentRuntime()
    rt.load_policy(app.POLICY_PATH)
    rt.register_tool(AnalyticsQueryTool(db_path=app.GOLD_DB, seed_demo=False,
                                        db_paths={k: v["db"] for k, v in app.DATASETS.items()}))
    tmp = tempfile.mkdtemp()
    agent = GovernedAgentGraph(runtime=rt, planner=planner,
                               checkpoint_db=os.path.join(tmp, "cp.sqlite"),
                               schema_docs={k: v["schema_doc"] for k, v in app.DATASETS.items()})
    state = agent.run(DENY, dataset=app._dataset(None))
    assert not state["allowed"] and state["attempts"] == agent.max_attempts, state
    state = agent.run(ASK, dataset=app._dataset(None))
    assert state["allowed"], state

    # the Review scenarios go through the approval graph, as /propose does
    os.environ["APPROVALS_DB"] = os.path.join(tmp, "approvals.sqlite")
    import approval_graph
    approval_graph.CHECKPOINT_DB = os.path.join(tmp, "acp.sqlite")
    approver = approval_graph.GovernedApprovalAgent(runtime=rt, planner=planner)
    for q in (PROPOSE, ESCALATE):
        assert approver.start(q)["status"] == "pending_approval", q

    store = replay.ReplayStore.from_env()
    new = set(store.entries) - before
    for k in new:
        store.entries[k]["authored"] = True
    with open(store.path, "w", encoding="utf-8") as f:
        json.dump(store.entries, f, indent=2, sort_keys=True)
    print(f"authored {len(new)} entries -> {store.path}")


if __name__ == "__main__":
    main()
