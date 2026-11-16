"""Backlog item 38: every audit event says which request it belongs to, who
asked (a fingerprint of the configured key, never the key) and from where."""

import hashlib
import json
import os
import sys

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)
from src.runtime import request_context  # noqa: E402

KEY = "test-key"
GOOD = {"X-API-Key": KEY}
SQL = {"sql": "SELECT count(*) AS n FROM gold_hospital_profile"}
FP = "api:" + hashlib.sha256(KEY.encode()).hexdigest()[:8]


@pytest.fixture
def client(tmp_path, monkeypatch):
    for k, v in {
        "API_KEY": KEY,
        "TRACE_DB": tmp_path / "t.sqlite",
        "CHECKPOINT_DB": tmp_path / "c.sqlite",
        "APPROVALS_DB": tmp_path / "a.sqlite",
        "AUDIT_LOG_DIR": tmp_path / "audit",
        "PLANNER_METRICS_LOG": tmp_path / "m.jsonl",
        "RATE_LIMIT_QUERY": "100000/minute",
        "RATE_LIMIT_READ": "100000/minute",
    }.items():
        monkeypatch.setenv(k, str(v))
    monkeypatch.delenv("MCP_API_KEY", raising=False)
    import importlib

    import src.tools.base as tool_base
    import tracing

    previous = tool_base._TOOL_REGISTRY.pop("analytics.query_aggregate", None)
    tracing.set_tracer(None)
    import app as app_module

    importlib.reload(app_module)
    c = TestClient(app_module.app)
    c.__enter__()
    try:
        yield c
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


def test_a_query_s_audit_events_carry_its_request_principal_and_address(client, tmp_path):
    r = client.post("/raw-sql", json=SQL, headers=GOOD)
    assert r.status_code == 200
    rid = r.headers["x-request-id"]
    events = [e for e in _audit(tmp_path) if e.get("request_id") == rid]
    # the tool ran on a worker thread: the context followed it there
    assert {e["event_type"] for e in events} >= {"tool_execution"}, _audit(tmp_path)
    for e in events:
        assert e["principal"] == FP
        assert e["source_ip"] == "testclient"
    assert KEY not in "".join(f.read_text() for f in (tmp_path / "audit").glob("*.jsonl"))


def test_two_requests_get_two_ids(client):
    a = client.post("/raw-sql", json=SQL, headers=GOOD).headers["x-request-id"]
    b = client.post("/raw-sql", json=SQL, headers=GOOD).headers["x-request-id"]
    assert a != b


def test_a_wrong_or_missing_key_is_named_without_the_key(client, tmp_path):
    client.post("/raw-sql", json=SQL, headers={"X-API-Key": "wrong-key-SECRETISH"})
    client.post("/raw-sql", json=SQL)
    fails = [e for e in _audit(tmp_path) if e["event_type"] == "auth_failure"]
    assert [e["principal"] for e in fails] == ["invalid-key", "anonymous"]
    text = "".join(f.read_text() for f in (tmp_path / "audit").glob("*.jsonl"))
    assert "SECRETISH" not in text
    assert hashlib.sha256(b"wrong-key-SECRETISH").hexdigest()[:8] not in text


@pytest.mark.parametrize(
    "given, kept",
    [
        ("client-req-0001", True),
        ("short", False),
        ("has space 12345", False),
        ("x" * 65, False),
        ("evil\\nINJECTED", False),
    ],
)
def test_a_caller_request_id_is_kept_only_when_it_is_a_plain_token(client, given, kept):
    r = client.get("/health", headers={"X-Request-ID": given})
    assert (r.headers["x-request-id"] == given) is kept
    assert len(r.headers.get_list("x-request-id")) == 1


def test_mcp_calls_are_attributed_too(client, tmp_path):
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "query_analytics", "arguments": {"sql": SQL["sql"]}},
    }
    h = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {KEY}",
    }
    r = client.post("/mcp/", json=body, headers=h)
    if r.status_code in (404, 503):
        pytest.skip(f"MCP mount not available here ({r.status_code})")
    assert r.status_code == 200, r.text
    rid = r.headers["x-request-id"]
    events = [e for e in _audit(tmp_path) if e.get("request_id") == rid]
    assert events, _audit(tmp_path)
    assert all(e["principal"] == FP and e["source_ip"] == "testclient" for e in events)


def test_outside_a_request_the_fields_are_empty(tmp_path):
    from src.runtime.audit_logger import AuditLogger

    log = AuditLogger(log_dir=tmp_path, agent_id="cli", max_buffer_size=1, enable_console=False)
    log.log_answer(capability="query", allowed=True, decided_by=None, version={}, trace_id="t1")
    rows = [json.loads(x) for f in tmp_path.glob("*.jsonl") for x in f.read_text().splitlines()]
    assert rows and all(r["request_id"] is None and r["principal"] is None for r in rows)


def test_principal_names_the_configured_key_it_matched():
    keys = {"api": "k1", "mcp": "k2"}
    assert request_context.principal("k2", keys).startswith("mcp:")
    assert request_context.principal("k3", keys) == "invalid-key"
    assert request_context.principal(None, keys) == "anonymous"
    assert request_context.principal("", {"api": ""}) == "anonymous"
    assert request_context.principal("x", {"api": ""}) == "invalid-key"  # unset key matches nothing


@pytest.mark.parametrize(
    "value, refused", [("*", True), ("10.0.0.1, *", True), ("10.0.0.0/16", False), ("", False)]
)
def test_a_wildcard_proxy_trust_list_refuses_to_start(monkeypatch, value, refused):
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", value)
    if refused:
        with pytest.raises(SystemExit):
            request_context.check_forwarded_allow_ips()
    else:
        request_context.check_forwarded_allow_ips()
