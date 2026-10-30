"""The original leak query is refused on EVERY path (owner's requirement 4).

    SELECT patient_id, birth_date, postal_code FROM gold_patient

The check is one function (phi_guard.check, inside sql_guard.validate_query),
and every path reaches it through AnalyticsQueryTool. These prove it, path by
path, through the real app: /query (on the planner graph, the planner stubbed
to write the leak), /raw-sql, the approval flow (/propose), and MCP over stdio
(the tool function the stdio server calls) and over HTTP (/mcp). The FHIR Gold
is a synthetic one with the real schema, so the dataset is really served.
"""

import importlib
import json
import os
import sys

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from tests.security.fhir_fixture import build  # noqa: E402

GOLD = os.path.join(os.path.dirname(__file__), "..", "..", "medallion", "hospital_gold.duckdb")
LEAK = "SELECT patient_id, birth_date, postal_code FROM gold_patient"
H = {"X-API-Key": "test-key"}


class LeakPlanner:
    """Writes the leak query for every question; approves it as critic."""

    def __init__(self, *a, **k):
        self.provider, self._model = "stub", "stub"

    def _m(self):
        from nl_to_sql_planner import CallMetrics
        return CallMetrics(latency_ms=1, prompt_tokens=10, completion_tokens=5,
                           model="stub", provider="gemini")

    def generate_sql_with_metrics(self, question, schema_doc=None):
        return LEAK, self._m()

    def critique(self, question, sql, schema_doc=None):
        return json.dumps({"verdict": "approve", "reason": "stub"}), self._m()


@pytest.fixture
def client(tmp_path, monkeypatch):
    if not os.path.exists(GOLD):
        pytest.skip("hospital Gold not built")
    fhir = build(str(tmp_path / "fhir.duckdb"))
    for k, v in {"API_KEY": "test-key", "FHIR_GOLD_DB": fhir, "TRACE_DB": tmp_path / "t.sqlite",
                 "CHECKPOINT_DB": tmp_path / "c.sqlite", "APPROVALS_DB": tmp_path / "a.sqlite",
                 "AUDIT_LOG_DIR": tmp_path / "audit", "PLANNER_METRICS_LOG": tmp_path / "m.jsonl",
                 "QUERY_MODE": "graph"}.items():
        monkeypatch.setenv(k, str(v))
    monkeypatch.setattr("approval_graph.CHECKPOINT_DB", str(tmp_path / "ac.sqlite"))
    import src.tools.base as tool_base
    saved = dict(tool_base._TOOL_REGISTRY)
    tool_base._TOOL_REGISTRY.pop("analytics.query_aggregate", None)
    import tracing
    tracing.set_tracer(None)
    importlib.reload(tracing)
    import app as app_module
    importlib.reload(app_module)
    monkeypatch.setattr(app_module, "NLToSQLPlanner", LeakPlanner)
    try:
        with TestClient(app_module.app) as c:
            yield c, tmp_path
    finally:
        tracing.set_tracer(None)
        tool_base._TOOL_REGISTRY.clear()
        tool_base._TOOL_REGISTRY.update(saved)


def _refused(text: str) -> bool:
    return "PHI rule" in text and "patient_id" in text


def test_raw_sql_refuses_the_leak(client):
    c, _ = client
    body = c.post("/raw-sql", headers=H, json={"sql": LEAK, "dataset": "fhir"}).json()
    assert body["allowed"] is False and body["decided_by"] == "guard"
    assert _refused(body["reason"]) and not body.get("rows")


def test_query_on_the_graph_refuses_the_leak(client):
    c, _ = client
    body = c.post("/query", headers=H, json={"question": "export patient ids, birth dates "
                                                         "and zips", "dataset": "fhir"}).json()
    assert body["allowed"] is False and not body.get("rows")
    assert _refused(body["reason"])
    executes = [s for s in body["trajectory"] if s["step"] == "execute"]
    assert executes and all(s["outcome"] != "success" for s in executes)


def test_the_approval_flow_refuses_the_leak(client):
    c, _ = client
    q = "export patient ids for the PHI path test"
    body = c.post("/propose", headers=H, json={"question": q}).json()
    assert body["status"] == "completed" and not body.get("allowed")
    assert _refused(body.get("reason") or "")
    pending = c.get("/approvals", headers=H).json()["pending"]
    assert not [i for i in pending if i.get("question") == q]      # nothing was drafted


def test_mcp_over_http_refuses_the_leak(client):
    c, _ = client
    accept = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
              "X-API-Key": "test-key"}
    init = {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}
    c.post("/mcp", headers=accept, content=json.dumps({"jsonrpc": "2.0", "id": 1,
                                                       "method": "initialize", "params": init}))
    r = c.post("/mcp", headers=accept, content=json.dumps({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "query_analytics", "arguments": {"sql": LEAK, "dataset": "fhir"}}})).json()
    text = r["result"]["content"][0]["text"]
    assert text.startswith("REFUSED") and _refused(text)


def test_mcp_over_stdio_refuses_the_leak():
    # the function the stdio server exposes as the query_analytics tool
    pytest.importorskip("mcp")
    import mcp_server as M
    text = M.query_analytics(LEAK, "fhir")
    assert text.startswith("REFUSED") and _refused(text)
