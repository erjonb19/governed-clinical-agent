"""Backlog item 63: a failed audit write is never silent -- a warning with the
exception type, and /health says whether auditing is on and how often it failed."""

import logging
import os
import sys

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


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


def test_health_reports_auditing(app_module):
    with TestClient(app_module.app) as c:
        h = c.get("/health").json()
    assert h["audit_enabled"] is True and "audit_failures" not in h


def test_a_failed_audit_write_warns_and_is_counted(app_module, caplog):
    with TestClient(app_module.app) as c:
        logger = app_module._STATE["runtime"].audit_logger

        def boom(**kw):
            raise OSError("disk full at /secret/path")

        logger.log_answer = boom
        with caplog.at_level(logging.WARNING, logger="governed.audit"):
            app_module._audit_answer(True, "executed", {}, "t1")  # must not raise
        m = c.get("/metrics", headers={"X-API-Key": "test-key"}).json()
    assert m["audit_failures"] == 1
    assert "audit write failed (answer): OSError" in caplog.text
    assert "/secret/path" not in caplog.text  # the type only, never the message


def test_an_unwritable_audit_dir_says_so(tmp_path, monkeypatch, capsys):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setenv("AUDIT_LOG_DIR", str(blocker / "audit"))
    for k, v in {
        "API_KEY": "test-key",
        "TRACE_DB": tmp_path / "t.sqlite",
        "CHECKPOINT_DB": tmp_path / "c.sqlite",
        "APPROVALS_DB": tmp_path / "a.sqlite",
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
        with TestClient(m.app) as c:
            h = c.get("/health").json()
        assert h["audit_enabled"] is False
        assert "audit log disabled" in capsys.readouterr().err
    finally:
        tracing.set_tracer(None)
        tool_base._TOOL_REGISTRY.clear()
        tool_base._TOOL_REGISTRY.update(saved)
