"""The planner graph is traced and checkpointed: a run can be looked up afterwards
and a run that died part-way can be resumed without repeating finished steps.

WHY THIS FILE EXISTS
Before PLAN v2 PR 3 the planner graph compiled with no checkpointer and recorded
nothing: `trajectory` lived in memory and was returned to the caller, so a run
that raised between `plan` and `execute` left no trace and could not be picked
up. Only the approval graph persisted anything.

No LLM: the planner and the analytics tool are stubs (the same shape as
tests/integration/test_escalation_resumes.py). What is under test is the trace
store and the checkpoint lifecycle, not SQL.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import tracing
from src.runtime.agent_runtime import AgentRuntime
from src.tools.base import BaseTool, ToolResult

POLICY = os.path.join(os.path.dirname(__file__), "..", "..", "medicare_policy.yaml")


class StubAnalyticsTool(BaseTool):
    """Succeeds on SELECT, fails (as the guard would) on anything mentioning bronze."""

    @property
    def name(self) -> str:
        return "analytics.query_aggregate"

    def execute(self, params):
        sql = params.get("sql", "")
        if "bronze" in sql:
            return ToolResult(success=False, error="sql_guard: table not on Gold allowlist")
        return ToolResult(success=True, output={
            "safe_sql": sql + " LIMIT 1000", "backend": "duckdb",
            "rows": [{"facility_name": "Mercy General", "rate": 21.4}],
            "row_count": 1,
        })


class Metrics:
    latency_ms = 7
    prompt_tokens = 100
    completion_tokens = 20
    total_tokens = 120
    est_cost_usd = 0.0001
    model = "stub"
    provider = "stub"


class StubPlanner:
    """Scripted answers, one per call; a `RuntimeError` entry raises instead."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = 0

    def generate_sql_with_metrics(self, prompt, schema_doc=None):
        self.calls += 1
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a, Metrics()


@pytest.fixture
def make_agent(tmp_path):
    import src.tools.base as tool_base
    from agent_graph import GovernedAgentGraph

    previous = tool_base._TOOL_REGISTRY.pop("analytics.query_aggregate", None)
    runtime = AgentRuntime()
    runtime.load_policy(POLICY)
    runtime.register_tool(StubAnalyticsTool())
    tracer = tracing.Tracer(str(tmp_path / "traces.sqlite"))
    agents = []

    def _make(planner):
        a = GovernedAgentGraph(runtime=runtime, planner=planner,
                               checkpoint_db=str(tmp_path / "checkpoints.sqlite"),
                               tracer=tracer)
        agents.append(a)
        return a

    try:
        yield _make, tracer
    finally:
        for a in agents:
            try:
                a._cm.__exit__(None, None, None)
            except Exception:
                pass
        tool_base._TOOL_REGISTRY.pop("analytics.query_aggregate", None)
        if previous is not None:
            tool_base._TOOL_REGISTRY["analytics.query_aggregate"] = previous


def test_a_run_is_traced_end_to_end(make_agent):
    make, tracer = make_agent
    agent = make(StubPlanner(["SELECT 1 AS n"]))
    state = agent.run("how many hospitals?")

    assert state["allowed"] and state["decided_by"] == "executed"
    tid = state["trace_id"]
    trace = tracer.get_trace(tid)
    assert trace is not None and trace["status"] == "ok"
    assert trace["attrs"]["question"] == "how many hospitals?"
    assert trace["attrs"]["decided_by"] == "executed"
    names = [s["name"] for s in trace["spans"]]
    assert names == ["plan", "guard", "execute", "answer"]
    plan = trace["spans"][0]
    assert plan["attrs"]["sql"] == "SELECT 1 AS n" and plan["attrs"]["model"] == "stub"
    guard = trace["spans"][1]
    assert guard["attrs"]["allowed"] is True
    # the execute event is nested under the guard span
    assert trace["spans"][2]["parent_id"] == guard["span_id"]
    assert trace["spans"][2]["attrs"]["row_count"] == 1


def test_a_denial_and_retry_are_both_in_the_trace(make_agent):
    make, tracer = make_agent
    agent = make(StubPlanner(["SELECT * FROM bronze_x", "SELECT 1 AS n"]))
    state = agent.run("list bronze")

    assert state["attempts"] == 2 and state["allowed"]
    names = [s["name"] for s in tracer.get_trace(state["trace_id"])["spans"]]
    assert names == ["plan", "guard", "retry", "plan", "guard", "execute", "answer"]
    denied = tracer.get_trace(state["trace_id"])["spans"][1]
    assert denied["attrs"]["allowed"] is False and denied["attrs"]["decided_by"] == "guard"


def test_no_result_rows_are_ever_written_to_the_trace(make_agent):
    make, tracer = make_agent
    agent = make(StubPlanner(["SELECT 1 AS n"]))
    state = agent.run("rows please")
    assert state["rows"]   # the caller still gets the rows
    import json
    import sqlite3

    con = sqlite3.connect(tracer.db_path)
    blobs = [r[0] for r in con.execute("SELECT attrs FROM spans")]
    blobs += [r[0] for r in con.execute("SELECT attrs FROM traces")]
    con.close()
    for b in blobs:
        assert "Mercy General" not in b
        assert "rows" not in json.loads(b)


def test_no_result_rows_are_ever_written_to_the_checkpoint(make_agent, tmp_path):
    make, _ = make_agent
    agent = make(StubPlanner(["SELECT 1 AS n"]))
    state = agent.run("rows please")
    assert state["rows"] == [{"facility_name": "Mercy General", "rate": 21.4}]
    assert agent._rows == {}   # nothing held after the run
    import sqlite3

    con = sqlite3.connect(str(tmp_path / "checkpoints.sqlite"))
    tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    blobs = []
    for t in tables:
        for row in con.execute(f"SELECT * FROM {t}"):
            blobs += [v if isinstance(v, bytes) else str(v).encode() for v in row if v is not None]
    con.close()
    assert blobs, "the run was checkpointed"
    assert not any(b"Mercy General" in b for b in blobs)


def test_a_run_leaves_the_callers_trace_context_alone(make_agent):
    make, _ = make_agent
    before = tracing.current_trace_id()   # other test files may leave one set
    state = make(StubPlanner(["SELECT 1 AS n"])).run("q")
    assert tracing.current_trace_id() == before != state["trace_id"]


def test_a_run_resumed_after_execute_says_its_rows_are_gone(make_agent):
    # rows are never checkpointed, so a run that died after execute cannot get
    # them back; it must not look like an empty answer
    make, tracer = make_agent
    agent = make(StubPlanner(["SELECT 1 AS n"]))
    real_event, failed = tracer.event, []

    def flaky_event(name, **attrs):
        if name == "answer" and not failed:
            failed.append(1)
            raise RuntimeError("died in finish")
        return real_event(name, **attrs)

    tracer.event = flaky_event
    with pytest.raises(RuntimeError):
        agent.run("q", trace_id="after-execute")
    state = agent.run(trace_id="after-execute")
    assert state["allowed"] and state["row_count"] == 1
    assert state["rows"] is None and state["rows_unavailable"] is True


def test_a_failed_run_resumes_from_its_checkpoint(make_agent):
    make, tracer = make_agent
    planner = StubPlanner([RuntimeError("provider down"), "SELECT 1 AS n"])
    agent = make(planner)

    tid = "resume-me"
    with pytest.raises(RuntimeError):
        agent.run("first try", trace_id=tid)
    assert planner.calls == 1
    assert tracer.get_trace(tid)["status"] == "error"
    assert agent.state(tid) == {
        "trace_id": tid, "exists": True, "finished": False, "next": ["plan"],
        "attempts": 0, "last_error": None, "decided_by": None, "allowed": None, "steps": 0,
    }

    # Same id, no question: continues from the checkpoint, so plan runs once more and
    # the run finishes. The trace is the same trace, now ok, with a resume marker.
    state = agent.run(trace_id=tid)
    assert state["allowed"] and state["trace_id"] == tid and planner.calls == 2
    trace = tracer.get_trace(tid)
    assert trace["status"] == "ok"
    names = [s["name"] for s in trace["spans"]]
    assert names[0] == "plan" and trace["spans"][0]["status"] == "error"
    assert "resume" in names and names[-1] == "answer"
    assert agent.state(tid)["finished"] is True

    # A finished run returns its saved state; nothing runs again. Its rows were
    # never checkpointed, and it says so rather than looking empty.
    again = agent.run(trace_id=tid)
    assert again["decided_by"] == "executed" and planner.calls == 2
    assert again["rows"] is None and again["rows_unavailable"] is True


def test_a_run_survives_an_unwritable_trace_store(make_agent, tmp_path):
    make, _ = make_agent
    agent = make(StubPlanner(["SELECT 1 AS n"]))
    # a file where the directory should be: every write fails, the run does not
    bad = tmp_path / "not-a-dir"
    bad.write_text("x")
    agent.tracer = tracing.Tracer(str(bad / "traces.sqlite"))
    state = agent.run("still answers")
    assert state["allowed"]
    assert agent.tracer.get_trace(state["trace_id"]) is None
