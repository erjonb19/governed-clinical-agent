"""Backlog item 57: /ui-config hands out the API key only with UI_PREFILL_KEY=1
AND a loopback client; otherwise never."""

import importlib
import os
import sys

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
KEY = "test-key-prefill"


@pytest.fixture(autouse=True)
def _registry():
    # app reloads register the analytics tool; leave the registry as found
    import src.tools.base as tool_base
    import tracing

    saved = dict(tool_base._TOOL_REGISTRY)
    tool_base._TOOL_REGISTRY.pop("analytics.query_aggregate", None)
    tracing.set_tracer(None)
    try:
        yield
    finally:
        tracing.set_tracer(None)
        tool_base._TOOL_REGISTRY.clear()
        tool_base._TOOL_REGISTRY.update(saved)


def _client(tmp_path, monkeypatch, client_host, **env):
    for k, v in {
        "API_KEY": KEY,
        "TRACE_DB": tmp_path / "t.sqlite",
        "CHECKPOINT_DB": tmp_path / "c.sqlite",
        "APPROVALS_DB": tmp_path / "a.sqlite",
        "AUDIT_LOG_DIR": tmp_path / "audit",
        "PLANNER_METRICS_LOG": tmp_path / "m.jsonl",
        **env,
    }.items():
        monkeypatch.setenv(k, str(v))
    if "UI_PREFILL_KEY" not in env:
        monkeypatch.delenv("UI_PREFILL_KEY", raising=False)
    import app as app_module

    importlib.reload(app_module)
    return TestClient(app_module.app, client=(client_host, 50000))


@pytest.mark.parametrize(
    "host, flag, expect_key",
    [
        ("127.0.0.1", None, False),  # the default: never
        ("127.0.0.1", "1", True),  # opted in, on this machine
        ("::1", "1", True),
        ("203.0.113.9", "1", False),  # opted in, but a remote caller
        ("127.0.0.1", "0", False),
        ("127.0.0.1", "maybe", False),
    ],
)
def test_the_key_is_prefilled_only_when_asked_and_local(
    tmp_path, monkeypatch, host, flag, expect_key
):
    env = {} if flag is None else {"UI_PREFILL_KEY": flag}
    c = _client(tmp_path, monkeypatch, host, **env)
    body = c.get("/ui-config").json()
    assert (body["prefill_key"] == KEY) is expect_key, body
    assert KEY not in str(body) or expect_key
