"""/query on the planner graph (PLAN v2 D2), through the real app and the real guard.

The app runs under Starlette's TestClient against the committed hospital Gold;
only the LLM is stubbed (app.NLToSQLPlanner is swapped before startup). So the
SQL really goes through sql_guard and DuckDB, and the response, the trace, the
cost log and QUERY_MODE=single are all checked end to end.
"""

import importlib
import json
import os
import sys

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

GOLD = os.path.join(os.path.dirname(__file__), "..", "..", "medallion", "hospital_gold.duckdb")
H = {"X-API-Key": "test-key"}
COUNT = "SELECT COUNT(*) AS n FROM gold_hospital_profile"


def _planner_class(script):
    """A planner whose answers are scripted per question: a list of
    ("sql", text) / ("verdict", text) / ("raise", exc) to hand out in order."""
    from nl_to_sql_planner import CallMetrics

    class Stub:
        def __init__(self, *a, **k):
            self.provider, self._model = "gemini", "stub"

        def _next(self, question):
            key = next(k for k in script if question.startswith(k))
            kind, value = script[key].pop(0)
            if kind == "raise":
                raise value
            return value, CallMetrics(latency_ms=3, prompt_tokens=200, completion_tokens=20,
                                      model="stub", provider="gemini")

        def generate_sql_with_metrics(self, question, schema_doc=None):
            return self._next(question)

        def critique(self, question, sql, schema_doc=None):
            text, m = self._next(question)
            return json.dumps({"verdict": text, "reason": f"critic says {text}"}), m
    return Stub


@pytest.fixture
def make_client(tmp_path, monkeypatch):
    if not os.path.exists(GOLD):
        pytest.skip("hospital Gold not built")
    for k, v in {"API_KEY": "test-key", "TRACE_DB": tmp_path / "traces.sqlite",
                 "CHECKPOINT_DB": tmp_path / "checkpoints.sqlite",
                 "APPROVALS_DB": tmp_path / "approvals.sqlite", "AUDIT_LOG_DIR": tmp_path / "audit",
                 "PLANNER_METRICS_LOG": tmp_path / "metrics.jsonl"}.items():
        monkeypatch.setenv(k, str(v))
    monkeypatch.delenv("CRITIC_ENABLED", raising=False)
    monkeypatch.delenv("CRITIC_ROUTING", raising=False)
    import src.tools.base as tool_base
    previous = tool_base._TOOL_REGISTRY.pop("analytics.query_aggregate", None)
    clients = []

    def make(script, mode="graph"):
        monkeypatch.setenv("QUERY_MODE", mode)
        tool_base._TOOL_REGISTRY.pop("analytics.query_aggregate", None)
        import tracing
        tracing.set_tracer(None)
        importlib.reload(tracing)
        import cost_tracker          # reads PLANNER_METRICS_LOG at import
        importlib.reload(cost_tracker)
        import app as app_module
        importlib.reload(app_module)
        monkeypatch.setattr(app_module, "NLToSQLPlanner", _planner_class(script))
        c = TestClient(app_module.app)
        c.__enter__()
        clients.append(c)
        return c, tmp_path

    try:
        yield make
    finally:
        for c in clients:
            c.__exit__(None, None, None)
        import tracing
        tracing.set_tracer(None)
        tool_base._TOOL_REGISTRY.pop("analytics.query_aggregate", None)
        if previous is not None:
            tool_base._TOOL_REGISTRY["analytics.query_aggregate"] = previous


def test_query_runs_the_graph_and_answers(make_client):
    c, tmp = make_client({"how many": [("sql", COUNT), ("verdict", "approve")]})
    assert c.get("/health").json()["query_mode"] == "graph"
    body = c.post("/query", json={"question": "how many hospitals?"}, headers=H).json()
    assert body["allowed"] and body["decided_by"] == "executed"
    assert body["rows"][0]["n"] > 0 and body["columns"] == ["n"]
    assert body["attempts"] == 1 and body["critic"] == "approve"
    assert body["planner_metrics"]["calls"] == 2
    assert body["dataset"] == "hospital"
    names = [s["name"] for s in c.get(f"/traces/{body['trace_id']}", headers=H).json()["spans"]]
    assert names == ["plan", "critic", "guard", "execute", "answer"]
    # both model calls reach the cost log behind /metrics
    lines = (tmp / "metrics.jsonl").read_text().splitlines()
    assert len(lines) == 2


def test_a_guard_denial_is_corrected_on_the_next_attempt(make_client):
    c, _ = make_client({"list": [("sql", "SELECT * FROM bronze_patient"), ("verdict", "approve"),
                                 ("sql", COUNT), ("verdict", "approve")]})
    body = c.post("/query", json={"question": "list patients"}, headers=H).json()
    assert body["allowed"] and body["attempts"] == 2
    execs = [s for s in body["trajectory"] if s["step"] == "execute"]
    assert execs[0]["decided_by"] == "guard" and execs[1]["outcome"] == "success"
    assert all("metrics" not in s for s in body["trajectory"])


def test_a_critic_revise_is_visible_and_corrected(make_client):
    c, _ = make_client({"count": [("sql", "SELECT 1 AS n"), ("verdict", "revise"),
                                  ("sql", COUNT), ("verdict", "approve")]})
    body = c.post("/query", json={"question": "count hospitals"}, headers=H).json()
    assert body["allowed"] and body["attempts"] == 2
    verdicts = [s["verdict"] for s in body["trajectory"] if s["step"] == "critic"]
    assert verdicts == ["revise", "approve"]


def test_a_planner_outage_is_a_clean_refusal_with_a_trace(make_client):
    c, _ = make_client({"down": [("raise", RuntimeError("Error code: 429"))]})
    body = c.post("/query", json={"question": "down?"}, headers=H).json()
    assert body["allowed"] is False and "planner error" in body["reason"]
    assert body["decided_by"] == "planner_error"     # not "policy": the guard never ran
    t = c.get(f"/traces/{body['trace_id']}", headers=H).json()
    assert t["status"] == "error"


def test_single_mode_keeps_the_old_path(make_client):
    c, _ = make_client({"how many": [("sql", COUNT)]}, mode="single")
    assert c.get("/health").json()["query_mode"] == "single"
    body = c.post("/query", json={"question": "how many hospitals?"}, headers=H).json()
    assert body["allowed"] and body.get("attempts") is None and body.get("critic") is None
    names = [s["name"] for s in c.get(f"/traces/{body['trace_id']}", headers=H).json()["spans"]]
    assert names == ["plan", "guard", "execute", "answer"]


def test_an_unanswerable_refusal_reaches_the_switch_dataset_card(make_client):
    # one scripted item: if the critic were consulted it would find nothing to
    # pop, fail open, and the step would not say "nothing to review"
    c, _ = make_client({"what does": [("sql", "SELECT 'no cost data in this dataset' AS unanswerable")]})
    body = c.post("/query", json={"question": "what does a knee replacement cost?"},
                  headers=H).json()
    assert body["unanswerable"] and body["rows"] == []
    crit = [s for s in body["trajectory"] if s["step"] == "critic"]
    assert crit and crit[0]["verdict"] == "approve" and "nothing to review" in crit[0]["reason"]


def test_an_execution_error_never_puts_a_value_into_state(make_client):
    bad = ("SELECT CAST(facility_name AS INTEGER) AS x FROM gold_hospital_profile "
           "WHERE facility_name IS NOT NULL ORDER BY facility_id LIMIT 1")
    c, tmp = make_client({"cast": [("sql", bad), ("verdict", "approve")] * 3})
    name = c.post("/raw-sql", headers=H, json={
        "sql": "SELECT facility_name FROM gold_hospital_profile WHERE facility_name IS NOT NULL "
               "ORDER BY facility_id LIMIT 1"}).json()["rows"][0]["facility_name"]
    body = c.post("/query", json={"question": "cast the names"}, headers=H).json()
    assert body["allowed"] is False
    assert name not in json.dumps(body)
    # nor in the checkpoint DB, which the retry prompt is built from
    raw = (tmp / "checkpoints.sqlite").read_bytes()
    assert name.encode() not in raw


def test_single_mode_names_a_database_failure_as_such(make_client):
    c, _ = make_client({"cast": [("sql", "SELECT CAST(city AS DATE) AS d FROM gold_hospital_profile "
                                          "ORDER BY facility_id LIMIT 1")]}, mode="single")
    body = c.post("/query", json={"question": "cast cities"}, headers=H).json()
    assert body["allowed"] is False and body["decided_by"] == "sql_error"
    assert "withheld" in body["reason"]

