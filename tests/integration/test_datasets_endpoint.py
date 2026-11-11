"""Backlog item 36: /datasets stays open (the UI's Data view and the nightly
live check load it with no key), but it is rate-limited and its per-table row
counts are computed once per data vintage, not on every request."""

import os
import sys

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)


def _client(tmp_path, monkeypatch, read_limit):
    for k, v in {
        "API_KEY": "test-key",
        "TRACE_DB": tmp_path / "t.sqlite",
        "CHECKPOINT_DB": tmp_path / "c.sqlite",
        "APPROVALS_DB": tmp_path / "a.sqlite",
        "AUDIT_LOG_DIR": tmp_path / "audit",
        "PLANNER_METRICS_LOG": tmp_path / "m.jsonl",
        "RATE_LIMIT_QUERY": "100000/minute",
        "RATE_LIMIT_READ": read_limit,
    }.items():
        monkeypatch.setenv(k, str(v))
    import importlib

    import src.tools.base as tool_base
    import tracing

    tool_base._TOOL_REGISTRY.pop("analytics.query_aggregate", None)
    tracing.set_tracer(None)
    import app as app_module

    importlib.reload(app_module)
    return TestClient(app_module.app), app_module


@pytest.fixture
def make(tmp_path, monkeypatch):
    import src.tools.base as tool_base
    import tracing

    previous = tool_base._TOOL_REGISTRY.get("analytics.query_aggregate")
    opened = []

    def factory(read_limit="100000/minute"):
        c, m = _client(tmp_path, monkeypatch, read_limit)
        c.__enter__()
        opened.append(c)
        return c, m

    yield factory
    for c in opened:
        c.__exit__(None, None, None)
    tracing.set_tracer(None)
    tool_base._TOOL_REGISTRY.pop("analytics.query_aggregate", None)
    if previous is not None:
        tool_base._TOOL_REGISTRY["analytics.query_aggregate"] = previous


def test_open_without_a_key_and_counts_are_cached_per_vintage(make, monkeypatch):
    c, app_module = make()
    calls = []
    real = app_module.run_governed_sql

    def counting(sql, **kw):
        calls.append(sql)
        return real(sql, **kw)

    monkeypatch.setattr(app_module, "run_governed_sql", counting)
    first = c.get("/datasets")
    assert first.status_code == 200, first.text
    tables = first.json()["tables"]
    assert tables and all(t["rows"] is not None for t in tables), tables
    n = len(calls)
    assert n == len(tables)
    for _ in range(5):
        r = c.get("/datasets")
        assert r.status_code == 200 and r.json()["tables"] == tables
    assert len(calls) == n, "the counts were recomputed on a repeat request"

    # a new data vintage (the Gold file changes) recomputes once
    db = app_module.DATASETS[first.json()["active_dataset"]]["db"]
    real_getmtime = os.path.getmtime
    monkeypatch.setattr(
        app_module.os.path, "getmtime", lambda p: real_getmtime(p) + (1000 if p == db else 0)
    )
    assert c.get("/datasets").json()["tables"] == tables
    assert len(calls) == 2 * n


def test_rate_limited(make):
    c, _ = make(read_limit="3/minute")
    codes = [c.get("/datasets").status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200] and codes[3:] == [429, 429], codes
