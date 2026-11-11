"""Security audit item 34 (SECURITY_STANDARDS 7.1, 8): request size limits.
An oversized body is refused with 413 before it is parsed; over-long fields
are refused with 422 before they reach a model prompt or the database."""

import os
import sys

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)
H = {"X-API-Key": "test-key"}


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

    import src.tools.base as tool_base
    import tracing

    previous = tool_base._TOOL_REGISTRY.pop("analytics.query_aggregate", None)
    tracing.set_tracer(None)
    import app as app_module

    importlib.reload(app_module)
    c = TestClient(app_module.app)
    c.__enter__()
    try:
        yield c, app_module
    finally:
        c.__exit__(None, None, None)
        tracing.set_tracer(None)
        tool_base._TOOL_REGISTRY.pop("analytics.query_aggregate", None)
        if previous is not None:
            tool_base._TOOL_REGISTRY["analytics.query_aggregate"] = previous


def test_an_oversized_body_is_refused_with_413(client):
    c, app_module = client
    r = c.post(
        "/raw-sql", json={"sql": "SELECT 1 -- " + "x" * (app_module.MAX_BODY_BYTES + 10)}, headers=H
    )
    assert r.status_code == 413, r.text


@pytest.mark.parametrize(
    "path,body",
    [
        ("/query", {"question": "q" * 2_001}),
        ("/raw-sql", {"sql": "SELECT 1 AS n -- " + "x" * 20_001}),
        ("/propose", {"question": "q" * 2_001}),
        ("/query", {"question": "ok", "dataset": "d" * 201}),
    ],
)
def test_over_long_fields_are_refused_with_422(client, path, body):
    c, _ = client
    r = c.post(path, json=body, headers=H)
    assert r.status_code == 422, (path, r.status_code, r.text[:200])


def test_a_decision_with_an_over_long_reason_is_refused(client):
    c, _ = client
    r = c.post(
        "/approvals/x",
        json={"decision": "reject", "decided_by": "a", "reason": "r" * 20_001},
        headers=H,
    )
    assert r.status_code == 422, r.text[:200]


def test_normal_requests_still_work(client):
    c, _ = client
    r = c.post(
        "/raw-sql", json={"sql": "SELECT count(*) AS n FROM gold_hospital_profile"}, headers=H
    )
    assert r.status_code == 200 and r.json()["allowed"] is True


def _chunks(total, size=8192):
    sent = 0
    while sent < total:
        n = min(size, total - sent)
        sent += n
        yield b" " * n


def test_a_chunked_oversized_body_is_refused_with_413(client):
    c, app_module = client
    total = app_module.MAX_BODY_BYTES + 10_000
    r = c.post(
        "/raw-sql", content=_chunks(total), headers={**H, "Content-Type": "application/json"}
    )
    assert r.status_code == 413, (r.status_code, r.text[:200])


def test_a_chunked_oversized_mcp_body_is_refused_with_413(client):
    c, app_module = client
    total = app_module.MAX_BODY_BYTES + 10_000
    r = c.post(
        "/mcp/",
        content=_chunks(total),
        headers={
            **H,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
    )
    if r.status_code in (404, 503):
        pytest.skip(f"MCP mount not available here ({r.status_code})")
    assert r.status_code == 413, (r.status_code, r.text[:200])


def test_a_small_chunked_body_still_works(client):
    c, _ = client
    body = b'{"sql": "SELECT count(*) AS n FROM gold_hospital_profile"}'
    r = c.post(
        "/raw-sql",
        content=iter([body[:20], body[20:]]),
        headers={**H, "Content-Type": "application/json"},
    )
    assert r.status_code == 200 and r.json()["allowed"] is True
