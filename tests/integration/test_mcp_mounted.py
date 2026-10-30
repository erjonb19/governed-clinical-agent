"""/mcp in the API (PLAN v2 D5): the MCP tools over Streamable HTTP, in-process.

Raw JSON-RPC through Starlette's TestClient against the real app and the
committed hospital Gold. The full client-library round trip over a real socket,
on all three transports, is tests/e2e/test_mcp_e2e.py.
"""

import importlib
import json
import os
import sys

import pytest

pytest.importorskip("mcp", reason="the MCP front door needs the mcp package")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

GOLD = os.path.join(os.path.dirname(__file__), "..", "..", "medallion", "hospital_gold.duckdb")
ACCEPT = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    if not os.path.exists(GOLD):
        pytest.skip("hospital Gold not built")
    for k, v in {"API_KEY": "api-key", "MCP_API_KEY": "mcp-key",
                 "TRACE_DB": tmp_path / "t.sqlite", "CHECKPOINT_DB": tmp_path / "c.sqlite",
                 "APPROVALS_DB": tmp_path / "a.sqlite", "AUDIT_LOG_DIR": tmp_path / "audit",
                 "PLANNER_METRICS_LOG": tmp_path / "m.jsonl"}.items():
        monkeypatch.setenv(k, str(v))
    import src.tools.base as tool_base
    saved = dict(tool_base._TOOL_REGISTRY)
    tool_base._TOOL_REGISTRY.pop("analytics.query_aggregate", None)
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
        tool_base._TOOL_REGISTRY.clear()
        tool_base._TOOL_REGISTRY.update(saved)


def rpc(c, method, params=None, key="mcp-key", rid=1):
    headers = dict(ACCEPT)
    if key:
        headers["Authorization"] = f"Bearer {key}"
    body = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}
    return c.post("/mcp", headers=headers, content=json.dumps(body))


INIT = {"protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"}}


def test_mcp_is_mounted_and_reported(client):
    h = client.get("/health").json()
    assert h["mcp_http"] is True and h["mcp_error"] is None


def test_the_mcp_key_is_required_and_the_api_key_is_not_it(client):
    assert rpc(client, "initialize", INIT, key=None).status_code == 401
    assert rpc(client, "initialize", INIT, key="api-key").status_code == 401
    r = client.post("/mcp", headers={**ACCEPT, "X-API-Key": "mcp-key"},
                    content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                        "params": INIT}))
    assert r.status_code == 200 and r.json()["result"]["serverInfo"]["name"] == "guarded-runtime"


def test_tools_list_and_a_guarded_call(client):
    assert rpc(client, "initialize", INIT).status_code == 200
    names = {t["name"] for t in rpc(client, "tools/list", rid=2).json()["result"]["tools"]}
    assert {"query_analytics", "analytics_schema"} <= names

    ok = rpc(client, "tools/call", {"name": "query_analytics", "arguments": {
        "sql": "SELECT COUNT(*) AS n FROM gold_hospital_profile"}}, rid=3).json()
    text = ok["result"]["content"][0]["text"]
    assert json.loads(text)["rows"][0]["n"] > 0

    bad = rpc(client, "tools/call", {"name": "query_analytics", "arguments": {
        "sql": "SELECT * FROM information_schema.tables"}}, rid=4).json()
    assert "REFUSED" in bad["result"]["content"][0]["text"]
    assert "catalog" in bad["result"]["content"][0]["text"]
