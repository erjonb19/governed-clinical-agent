"""An escalated item must still be executable by whoever inherits it.

WHY THIS FILE EXISTS
The store-level fix (tests/unit/test_approval_escalation.py) keeps the item in
the queue. That alone is not enough, because the queue entry and the paused
graph are two different pieces of state.

`resume()` used to invoke the graph for EVERY decision. Resuming consumes the
interrupt: the graph runs on to `commit`, sees a verdict that is not in
EXECUTES, finishes as not-committed, and the thread is spent. So even with the
item back in the queue, the new owner's approval would have had no execution
left to authorise -- it would record 'approved' against a graph that had already
finished as 'not committed'.

This exercises the real LangGraph checkpointer through a full
escalate-then-approve handoff. No LLM: the planner and the analytics tool are
stubs, because what is under test is the interrupt lifecycle, not SQL.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from approval import ApprovalStore, APPROVE, ESCALATE, REJECT
from src.runtime.agent_runtime import AgentRuntime
from src.tools.base import BaseTool, ToolResult

POLICY = os.path.join(os.path.dirname(__file__), "..", "..", "medicare_policy.yaml")


class StubAnalyticsTool(BaseTool):
    """Stands in for AnalyticsQueryTool. The guard is tested elsewhere; here the
    query only needs to succeed so the graph reaches the approval checkpoint."""

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

    # Both SQLite files must land in the tmp dir. The store's path is bound as a
    # default argument at import time, so redirect the constructor rather than
    # the module constant.
    store = ApprovalStore(str(tmp_path / "approvals.sqlite"))
    monkeypatch.setattr(approval_graph, "ApprovalStore", lambda *a, **k: store)
    monkeypatch.setattr(approval_graph, "CHECKPOINT_DB",
                        str(tmp_path / "checkpoints.sqlite"))

    # The tool registry is process-global and refuses a duplicate name, so a
    # per-test registration fails on the second test in the file. Drop any
    # existing entry first and put it back afterwards, leaving the registry as
    # this test found it for whatever runs next in the same process.
    import src.tools.base as tool_base
    previous = tool_base._TOOL_REGISTRY.pop("analytics.query_aggregate", None)

    runtime = AgentRuntime()
    runtime.load_policy(POLICY)
    runtime.register_tool(StubAnalyticsTool())

    a = approval_graph.GovernedApprovalAgent(
        runtime=runtime, planner=StubPlanner(), capability="brief.commit")
    try:
        yield a
    finally:
        a._cm.__exit__(None, None, None)
        tool_base._TOOL_REGISTRY.pop("analytics.query_aggregate", None)
        if previous is not None:
            tool_base._TOOL_REGISTRY["analytics.query_aggregate"] = previous


def _start(agent):
    started = agent.start("Which hospitals have the worst readmission rates?")
    assert started["status"] == "pending_approval"
    return started["thread_id"]


# --------------------------------------------------------------------------

def test_escalation_does_not_consume_the_interrupt(agent):
    """The defect, at the graph level: the handoff must leave the thread parked
    exactly where it was."""
    thread = _start(agent)

    out = agent.resume(thread, ESCALATE, "nurse.a",
                       reason="above my level", escalated_to="dr.b")
    assert out["status"] == "reassigned"
    assert out["still_pending"] is True
    assert out["committed"] is False
    assert out["assigned_to"] == "dr.b"

    # The item is back in the queue, owned by someone else...
    pend = agent.store.pending()
    assert len(pend) == 1 and pend[0]["assigned_to"] == "dr.b"

    # ...and the new owner's approval still reaches the action.
    final = agent.resume(thread, APPROVE, "dr.b", reason="reviewed")
    assert final["status"] == "resumed"
    assert final["committed"] is True


def test_escalation_chain_then_approve(agent):
    thread = _start(agent)
    agent.resume(thread, ESCALATE, "nurse.a", escalated_to="dr.b")
    agent.resume(thread, ESCALATE, "dr.b", escalated_to="compliance.c")

    final = agent.resume(thread, APPROVE, "compliance.c")
    assert final["committed"] is True

    approval_id = agent.store.find_by_thread(thread)
    assert [e["decision"] for e in agent.store.history(approval_id)] == [
        "escalate", "escalate", "approve"]


def test_reject_after_escalation_does_not_execute(agent):
    """The inherited item is a real decision, not a rubber stamp -- the new
    owner can refuse it, and that refusal is terminal."""
    thread = _start(agent)
    agent.resume(thread, ESCALATE, "nurse.a", escalated_to="dr.b")

    final = agent.resume(thread, REJECT, "dr.b", reason="not clinically indicated")
    assert final["committed"] is False
    assert final["still_pending"] is False
    assert agent.store.pending() == []


def test_escalation_does_not_mint_a_second_approval_record(agent):
    """`_propose` is idempotent per thread. Escalation must not defeat that by
    creating a fresh record for the same paused graph."""
    thread = _start(agent)
    agent.resume(thread, ESCALATE, "nurse.a", escalated_to="dr.b")
    agent.resume(thread, APPROVE, "dr.b")

    with agent.store._conn() as con:
        n = con.execute("SELECT count(*) c FROM approvals WHERE thread_id = ?",
                        (thread,)).fetchone()["c"]
    assert n == 1


def test_terminal_decision_still_ends_the_thread(agent):
    """Guard against over-correcting: only escalation is non-terminal."""
    thread = _start(agent)
    out = agent.resume(thread, APPROVE, "dr.b")
    assert out["still_pending"] is False
    assert agent.store.pending() == []
