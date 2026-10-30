"""/raw-sql and /query leave a trace, and /traces reads it back behind the API key.

Runs the real app through Starlette's TestClient against the committed hospital
Gold, with no LLM key (so /query reports the planner as unavailable and the
approval agent is off). The trace and checkpoint stores are redirected to a tmp
dir. httpx ships with the pinned `anthropic` SDK; skip cleanly if it is absent.
"""

import importlib
import os
import sys

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

GOLD = os.path.join(os.path.dirname(__file__), "..", "..", "medallion", "hospital_gold.duckdb")


@pytest.fixture
def client(tmp_path, monkeypatch):
    if not os.path.exists(GOLD):
        pytest.skip("hospital Gold not built")
    monkeypatch.setenv("API_KEY", "test-key")
    monkeypatch.setenv("TRACE_DB", str(tmp_path / "traces.sqlite"))
    monkeypatch.setenv("CHECKPOINT_DB", str(tmp_path / "checkpoints.sqlite"))
    monkeypatch.setenv("APPROVALS_DB", str(tmp_path / "approvals.sqlite"))
    monkeypatch.setenv("AUDIT_LOG_DIR", str(tmp_path / "audit"))
    monkeypatch.setenv("PLANNER_METRICS_LOG", str(tmp_path / "metrics.jsonl"))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CEREBRAS_API_KEY", raising=False)
    monkeypatch.setenv("PLANNER_PROVIDER", "gemini")

    import src.tools.base as tool_base
    previous = tool_base._TOOL_REGISTRY.pop("analytics.query_aggregate", None)
    import tracing
    tracing.set_tracer(None)
    importlib.reload(tracing)
    import app as app_module
    importlib.reload(app_module)
    try:
        with TestClient(app_module.app) as c:
            yield c
    finally:
        tracing.set_tracer(None)
        tool_base._TOOL_REGISTRY.pop("analytics.query_aggregate", None)
        if previous is not None:
            tool_base._TOOL_REGISTRY["analytics.query_aggregate"] = previous


H = {"X-API-Key": "test-key"}


def test_raw_sql_is_traced_and_readable(client):
    r = client.post("/raw-sql", json={"sql": "SELECT COUNT(*) AS n FROM gold_hospital_profile"},
                    headers=H)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["allowed"] and body["trace_id"]
    tid = body["trace_id"]

    listed = client.get("/traces", headers=H).json()
    assert [t["trace_id"] for t in listed["traces"]][0] == tid
    assert listed["traces"][0]["attrs"]["mode"] == "raw-sql"

    trace = client.get(f"/traces/{tid}", headers=H).json()
    assert trace["status"] == "ok" and trace["attrs"]["decided_by"] == "executed"
    assert [s["name"] for s in trace["spans"]] == ["guard", "execute", "answer"]
    assert trace["spans"][0]["attrs"]["allowed"] is True
    assert trace["spans"][1]["attrs"]["row_count"] == 1
    # the trace holds the hash and a count, never the result
    assert "rows" not in trace["spans"][1]["attrs"]
    assert trace["spans"][0]["attrs"]["sql_sha256"]


def test_a_guard_denial_is_traced(client):
    r = client.post("/raw-sql", json={"sql": "SELECT * FROM information_schema.tables"},
                    headers=H)
    body = r.json()
    assert body["allowed"] is False
    trace = client.get(f"/traces/{body['trace_id']}", headers=H).json()
    assert [s["name"] for s in trace["spans"]] == ["guard", "answer"]
    assert trace["spans"][0]["attrs"]["allowed"] is False
    assert trace["spans"][1]["attrs"]["outcome"] == "refused"


def test_traces_need_the_api_key(client):
    assert client.get("/traces").status_code == 401
    assert client.get("/traces/anything").status_code == 401
    assert client.get("/traces/anything", headers=H).status_code == 404


def test_audit_log_is_written_by_the_service(client, tmp_path):
    client.post("/raw-sql", json={"sql": "SELECT 1 AS n"}, headers=H)
    files = list((tmp_path / "audit").glob("audit_api_*.jsonl"))
    assert files, "the runtime's audit logger is attached on the service path"
    assert files[0].read_text().strip()
