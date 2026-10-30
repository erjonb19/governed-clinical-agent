"""Author the recordings a well-behaved model will not produce on cue.

The guard-denial scenario needs SQL that reads bronze_patient on EVERY attempt,
so the run ends refused, naming the rule, with its retries in the trace. A real
model, told the allowlist, declines to write that. So these entries are written
here instead of recorded, and marked "authored": true.

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

from test_ui_e2e import DENY                            # noqa: E402

BRONZE_SQL = "SELECT * FROM bronze_patient LIMIT 10"
APPROVE = json.dumps({"verdict": "approve", "reason": "authored for the guard-denial scenario",
                      "issues": []})


class Scripted:
    """Stands in for the real provider behind the replay planner."""

    def _complete(self, system, user, role):
        from nl_to_sql_planner import CallMetrics
        text = APPROVE if role == "critic" else BRONZE_SQL
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

    store = replay.ReplayStore.from_env()
    new = set(store.entries) - before
    for k in new:
        store.entries[k]["authored"] = True
    with open(store.path, "w", encoding="utf-8") as f:
        json.dump(store.entries, f, indent=2, sort_keys=True)
    print(f"authored {len(new)} entries for {DENY!r} -> {store.path}")


if __name__ == "__main__":
    main()
