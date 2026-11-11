"""Backlog item 35: failed API-key attempts are counted, audited and throttled
per client, on the API and on MCP over HTTP. The key never reaches the log."""

import json
import os
import sys

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)
GOOD = {"X-API-Key": "test-key"}
BAD = {"X-API-Key": "wrong-key-SECRETISH"}
SQL = {"sql": "SELECT count(*) AS n FROM gold_hospital_profile"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    for k, v in {
        "API_KEY": "test-key",
        "TRACE_DB": tmp_path / "t.sqlite",
        "CHECKPOINT_DB": tmp_path / "c.sqlite",
        "APPROVALS_DB": tmp_path / "a.sqlite",
        "AUDIT_LOG_DIR": tmp_path / "audit",
        "PLANNER_METRICS_LOG": tmp_path / "m.jsonl",
        "RATE_LIMIT_QUERY": "100000/minute",
        "RATE_LIMIT_READ": "100000/minute",
    }.items():
        monkeypatch.setenv(k, str(v))
    import importlib

    import auth_failures
    import src.tools.base as tool_base
    import tracing

    previous = tool_base._TOOL_REGISTRY.pop("analytics.query_aggregate", None)
    tracing.set_tracer(None)
    import app as app_module

    importlib.reload(app_module)
    auth_failures.FAILURES.per_window = 3
    c = TestClient(app_module.app)
    c.__enter__()
    try:
        yield c, auth_failures.FAILURES
    finally:
        c.__exit__(None, None, None)
        tracing.set_tracer(None)
        tool_base._TOOL_REGISTRY.pop("analytics.query_aggregate", None)
        if previous is not None:
            tool_base._TOOL_REGISTRY["analytics.query_aggregate"] = previous


def _audit(tmp_path):
    return [
        json.loads(line)
        for f in (tmp_path / "audit").glob("audit_api_*.jsonl")
        for line in f.read_text().splitlines()
        if line.strip()
    ]


def test_each_refused_key_is_audited_without_the_key(client, tmp_path):
    c, failures = client
    assert c.post("/raw-sql", json=SQL, headers=BAD).status_code == 401
    assert c.post("/raw-sql", json=SQL).status_code == 401  # missing key
    events = [e for e in _audit(tmp_path) if e["event_type"] == "auth_failure"]
    assert len(events) == 2, events
    assert events[-1]["context"]["failures_in_window"] == 2
    assert events[-1]["context"]["path"] == "/raw-sql"
    assert events[-1]["context"]["surface"] == "api"
    assert events[-1]["decision"] == "deny"
    text = "".join(f.read_text() for f in (tmp_path / "audit").glob("*.jsonl"))
    assert "SECRETISH" not in text and "wrong-key" not in text


def test_by_default_it_is_audit_only_and_the_ui_still_sees_401(client, tmp_path):
    c, failures = client
    codes = [c.post("/raw-sql", json=SQL, headers=BAD).status_code for _ in range(5)]
    assert codes == [401] * 5, codes  # no 429: one scanner must not confuse everyone
    assert c.post("/raw-sql", json=SQL).status_code == 401
    ok = c.post("/raw-sql", json=SQL, headers=GOOD)
    assert ok.status_code == 200 and ok.json()["allowed"] is True
    events = [e for e in _audit(tmp_path) if e["event_type"] == "auth_failure"]
    assert [e["context"]["throttled"] for e in events] == [False] * 3 + [True] * 3


def test_lockout_mode_throttles_every_request_from_the_client(client):
    c, failures = client
    failures.lockout = True
    codes = [c.post("/raw-sql", json=SQL, headers=BAD).status_code for _ in range(5)]
    assert codes == [401, 401, 401, 429, 429], codes
    r = c.post("/raw-sql", json=SQL, headers=GOOD)
    assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1


def test_the_window_expires(client):
    c, failures = client
    failures.lockout = True
    now = [1000.0]
    failures.clock = lambda: now[0]
    for _ in range(3):
        c.post("/raw-sql", json=SQL, headers=BAD)
    assert c.post("/raw-sql", json=SQL, headers=BAD).status_code == 429
    now[0] += failures.window_sec + 1
    assert c.post("/raw-sql", json=SQL, headers=BAD).status_code == 401


def test_mcp_refused_keys_are_counted_and_throttled_under_lockout(client, tmp_path):
    c, failures = client
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    h = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}

    def codes(n):
        return [
            c.post("/mcp/", json=body, headers={**h, "X-API-Key": "nope"}).status_code
            for _ in range(n)
        ]

    first = codes(4)
    if first[0] in (404, 503):
        pytest.skip(f"MCP mount not available here ({first[0]})")
    assert first == [401] * 4, first  # default: audit only
    events = [e for e in _audit(tmp_path) if e["event_type"] == "auth_failure"]
    assert len(events) == 4 and all(e["context"]["surface"] == "mcp" for e in events)
    failures.lockout = True
    assert codes(1) == [429]


def test_a_valid_key_is_never_counted(client):
    c, failures = client
    for _ in range(10):
        assert c.post("/raw-sql", json=SQL, headers=GOOD).status_code == 200
    assert failures.total == 0
