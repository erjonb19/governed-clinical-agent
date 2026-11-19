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
    from src.runtime import audit_logger

    importlib.reload(m)
    audit_logger.reset_failures()
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
    assert m["audit_failures"] == 1 and m["audit_error"] is None
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
            m2 = c.get("/metrics", headers={"X-API-Key": "test-key"}).json()
        assert h["audit_enabled"] is False
        assert m2["audit_error"] in {"NotADirectoryError", "FileExistsError", "OSError"}
        assert "audit log disabled" in capsys.readouterr().err
    finally:
        tracing.set_tracer(None)
        tool_base._TOOL_REGISTRY.clear()
        tool_base._TOOL_REGISTRY.update(saved)


def _boom(**kw):
    raise OSError("disk full at /secret/path")


def test_a_failed_critic_audit_is_counted(caplog):
    import critic
    from src.runtime import audit_logger

    audit_logger.reset_failures()

    class Logger:
        log_critic_review = staticmethod(_boom)

    class Runtime:
        audit_logger = Logger()

    with caplog.at_level(logging.WARNING, logger="governed.audit"):
        critic.audit(Runtime(), "cap", {"verdict": "approve"}, None)  # must not raise
    assert audit_logger.failure_count() == 1
    assert "audit write failed (critic_review): OSError" in caplog.text
    assert "/secret/path" not in caplog.text


def test_a_failed_refused_key_audit_is_counted_without_a_traceback(caplog):
    import auth_failures
    from src.runtime import audit_logger

    audit_logger.reset_failures()

    class Logger:
        log_auth_failure = staticmethod(_boom)

    f = auth_failures.AuthFailures(per_window=10, window_sec=60, lockout=False)
    f.audit = Logger()
    with caplog.at_level(logging.WARNING):
        assert f.record("203.0.113.9", "/query", "api") == 1  # the refusal still counts
    assert audit_logger.failure_count() == 1
    assert "audit write failed (auth_failure): OSError" in caplog.text
    assert "/secret/path" not in caplog.text and "Traceback" not in caplog.text
