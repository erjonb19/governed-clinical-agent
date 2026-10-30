"""An approval's trace id is its checkpoint thread id, so a decision submitted
later appends to the same trace, and the trace tells you where the thread stands.

Same stubs as test_escalation_resumes.py; what is under test is the trace
written around the interrupt lifecycle, not SQL.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import tracing
from approval import ApprovalStore, APPROVE, ESCALATE
from src.runtime.agent_runtime import AgentRuntime
from src.tools.base import BaseTool, ToolResult

POLICY = os.path.join(os.path.dirname(__file__), "..", "..", "medicare_policy.yaml")


class StubAnalyticsTool(BaseTool):
    @property
    def name(self) -> str:
        return "analytics.query_aggregate"

    def execute(self, params):
        return ToolResult(success=True, output={
            "safe_sql": "SELECT 1 AS n LIMIT 1000",
            "rows": [{"facility_name": "Mercy General", "rate": 21.4}],
            "row_count": 1,
        })


class StubPlanner:
    def generate_sql_with_metrics(self, prompt):
        return "SELECT 1 AS n", {"latency_ms": 0, "tokens": 0}


@pytest.fixture
def agent(tmp_path, monkeypatch):
    import approval_graph
    import src.tools.base as tool_base

    store = ApprovalStore(str(tmp_path / "approvals.sqlite"))
    monkeypatch.setattr(approval_graph, "ApprovalStore", lambda *a, **k: store)
    monkeypatch.setattr(approval_graph, "CHECKPOINT_DB", str(tmp_path / "checkpoints.sqlite"))
    previous = tool_base._TOOL_REGISTRY.pop("analytics.query_aggregate", None)
    runtime = AgentRuntime()
    runtime.load_policy(POLICY)
    runtime.register_tool(StubAnalyticsTool())
    tracer = tracing.Tracer(str(tmp_path / "traces.sqlite"))
    a = approval_graph.GovernedApprovalAgent(
        runtime=runtime, planner=StubPlanner(), capability="brief.commit", tracer=tracer)
    try:
        yield a
    finally:
        a._cm.__exit__(None, None, None)
        tool_base._TOOL_REGISTRY.pop("analytics.query_aggregate", None)
        if previous is not None:
            tool_base._TOOL_REGISTRY["analytics.query_aggregate"] = previous


def test_propose_escalate_approve_is_one_trace(agent):
    started = agent.start("Which hospitals have the worst readmission rates?")
    thread = started["thread_id"]
    assert started["status"] == "pending_approval" and started["trace_id"] == thread

    t = agent.tracer.get_trace(thread)
    assert t["status"] == "pending_approval"
    assert t["attrs"]["approval_id"] == started["approval_id"]
    assert [s["name"] for s in t["spans"]] == ["plan", "guard", "execute", "approval.propose"]
    assert agent.state(thread) == {
        "thread_id": thread, "exists": True, "finished": False, "next": ["propose"],
        "attempts": 1, "last_error": None, "approval_id": None, "committed": False, "steps": 2,
    }

    # Escalation is recorded on the same trace and leaves it pending.
    agent.resume(thread, ESCALATE, "nurse.a", reason="above my level", escalated_to="dr.b")
    t = agent.tracer.get_trace(thread)
    assert t["status"] == "pending_approval"
    last = t["spans"][-1]
    assert last["name"] == "approval.decision" and last["attrs"]["decision"] == ESCALATE
    assert last["attrs"]["terminal"] is False and last["attrs"]["escalated_to"] == "dr.b"

    # The inheriting approver's decision resumes the graph and closes the trace.
    out = agent.resume(thread, APPROVE, "dr.b")
    assert out["committed"] is True
    t = agent.tracer.get_trace(thread)
    assert t["status"] == "ok" and t["attrs"]["committed"] is True
    names = [s["name"] for s in t["spans"]]
    assert names[-2:] == ["approval.decision", "answer"]
    assert t["spans"][-1]["attrs"] == {"outcome": "committed", "verdict": APPROVE,
                                       "decided_by": "human:dr.b"}
    # propose was recorded once, even though the node re-executed on resume
    assert names.count("approval.propose") == 1
    assert agent.state(thread)["finished"] is True

    # no evidence rows in the trace store
    import sqlite3
    con = sqlite3.connect(agent.tracer.db_path)
    blobs = [r[0] for r in con.execute("SELECT attrs FROM spans")]
    con.close()
    assert all("Mercy General" not in b for b in blobs)


def test_start_leaves_the_callers_trace_context_alone(agent):
    before = tracing.current_trace_id()
    started = agent.start("Which hospitals have the worst readmission rates?")
    assert tracing.current_trace_id() == before != started["thread_id"]
