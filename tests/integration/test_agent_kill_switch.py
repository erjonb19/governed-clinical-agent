"""Item 69: one switch stops the agent, and the MCP demo tools are opt-in.

AGENT_DISABLED=1 makes /query, /propose and /mcp (and their /v1 forms) answer 503
before any model call or tool runs; /health, /ui and the fixed, keyed /raw-sql keep
serving so an operator can still look. The Milestone-1 demo tools (echo,
fetch_url, git_push) are advertised over MCP only with MCP_DEMO_TOOLS=1.
"""

import os
import subprocess
import sys

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)
H = {"X-API-Key": "test-key"}


@pytest.fixture
def app_module(tmp_path, monkeypatch):
    for k, v in {
        "API_KEY": "test-key",
        "TRACE_DB": tmp_path / "t.sqlite",
        "CHECKPOINT_DB": tmp_path / "c.sqlite",
        "APPROVALS_DB": tmp_path / "a.sqlite",
        "AUDIT_LOG_DIR": tmp_path / "audit",
        "PLANNER_METRICS_LOG": tmp_path / "m.jsonl",
    }.items():
        monkeypatch.setenv(k, str(v))
    import importlib

    import src.tools.base as tool_base
    import tracing

    saved = dict(tool_base._TOOL_REGISTRY)
    tool_base._TOOL_REGISTRY.pop("analytics.query_aggregate", None)
    tracing.set_tracer(None)
    import app as m

    importlib.reload(m)
    try:
        yield m
    finally:
        tracing.set_tracer(None)
        tool_base._TOOL_REGISTRY.clear()
        tool_base._TOOL_REGISTRY.update(saved)


@pytest.mark.parametrize(
    "path", ["/query", "/v1/query", "/propose", "/v1/propose", "/mcp", "/v1/mcp", "/query/"]
)
def test_the_kill_switch_stops_every_agent_endpoint(app_module, monkeypatch, path):
    monkeypatch.setenv("AGENT_DISABLED", "1")
    with TestClient(app_module.app) as c:
        r = c.post(path, headers=H, json={"question": "how many hospitals?"})
    assert r.status_code == 503 and "AGENT_DISABLED" in r.text
    assert r.headers.get("retry-after")


def test_the_rest_keeps_serving_while_the_agent_is_off(app_module, monkeypatch):
    monkeypatch.setenv("AGENT_DISABLED", "1")
    with TestClient(app_module.app) as c:
        h = c.get("/health")
        assert h.status_code == 200 and h.json()["agent_disabled"] is True
        assert c.get("/ui").status_code == 200
        assert c.post("/raw-sql", headers=H, json={"sql": "SELECT 1"}).status_code != 503


def test_without_the_switch_nothing_is_blocked(app_module, monkeypatch):
    monkeypatch.delenv("AGENT_DISABLED", raising=False)
    with TestClient(app_module.app) as c:
        assert c.get("/health").json()["agent_disabled"] is False
        assert c.post("/query", headers=H, json={}).status_code != 503  # a 422, not off


def _advertised(env_value):
    env = {k: v for k, v in os.environ.items() if k != "MCP_DEMO_TOOLS"}
    if env_value is not None:
        env["MCP_DEMO_TOOLS"] = env_value
    code = (
        "import anyio, mcp_server as M; print(sorted(t.name for t in anyio.run(M.mcp.list_tools)))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120
    )
    assert out.returncode == 0, out.stderr[-2000:]
    return out.stdout.strip().splitlines()[-1]


def test_mcp_demo_tools_are_hidden_by_default():
    names = _advertised(None)
    assert "query_analytics" in names and "analytics_schema" in names
    for demo in ("echo", "fetch_url", "git_push"):
        assert demo not in names


def test_mcp_demo_tools_come_back_with_the_flag():
    names = _advertised("1")
    for demo in ("echo", "fetch_url", "git_push"):
        assert demo in names
