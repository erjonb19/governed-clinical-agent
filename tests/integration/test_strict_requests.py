"""Backlog item 55: request bodies refuse unknown fields (422), and the findings
and approval-queue routes declare their responses."""

import os
import sys

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
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
        yield c
    finally:
        c.__exit__(None, None, None)
        tracing.set_tracer(None)
        tool_base._TOOL_REGISTRY.pop("analytics.query_aggregate", None)
        if previous is not None:
            tool_base._TOOL_REGISTRY["analytics.query_aggregate"] = previous


@pytest.mark.parametrize(
    "path, body",
    [
        ("/raw-sql", {"sql": "SELECT 1", "sqll": "x"}),
        ("/query", {"question": "how many hospitals?", "bypass_guard": True}),
        ("/propose", {"question": "q", "proposed_by": "a", "approved": True}),
        ("/approvals/x", {"decision": "approve", "decided_by": "b", "role": "admin"}),
    ],
)
def test_an_unknown_field_is_refused(client, path, body):
    r = client.post(path, json=body, headers=H)
    assert r.status_code == 422, (path, r.status_code, r.text)
    assert "extra" in r.text.lower() or "not permitted" in r.text.lower()


def test_known_fields_still_work(client):
    r = client.post("/raw-sql", json={"sql": "SELECT 1 AS n"}, headers=H)
    assert r.status_code == 200, r.text


def test_findings_and_queue_declare_their_responses(client):
    import app as app_module

    routes = {r.path: r for r in app_module.app.routes if hasattr(r, "response_model")}
    for path in ("/findings", "/public/findings", "/approvals", "/raw-sql", "/query"):
        assert routes[path].response_model is not None, path


def test_the_queue_still_returns_its_keys(client):
    r = client.get("/approvals", headers=H)
    if r.status_code == 503:
        pytest.skip("approvals unavailable here")
    assert r.status_code == 200, r.text
    assert set(r.json()) == {"pending", "metrics"}
