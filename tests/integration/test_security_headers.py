"""Security audit item 33 (SECURITY_STANDARDS 9): security headers on every
response, and a strict Content Security Policy on the UI page whose script
hash matches the page actually served."""

import base64
import hashlib
import os
import re
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


COMMON = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "strict-origin-when-cross-origin",
}


@pytest.mark.parametrize(
    "path,headers",
    [("/health", {}), ("/v1/health", {}), ("/ui", {}), ("/traces", H), ("/nope", {})],
)
def test_every_response_carries_the_security_headers(client, path, headers):
    r = client.get(path, headers=headers)
    for k, v in COMMON.items():
        assert r.headers.get(k) == v, (path, k, r.headers.get(k))
    assert "max-age=" in r.headers.get("strict-transport-security", "")
    assert "camera=()" in r.headers.get("permissions-policy", "")


def test_the_ui_csp_allows_exactly_the_page_s_own_inline_script(client):
    r = client.get("/ui")
    csp = r.headers["content-security-policy"]
    import app as app_module

    scripts = app_module.inline_scripts(r.text)
    assert scripts, "the page has its inline script"
    for body in scripts:
        h = base64.b64encode(hashlib.sha256(body.encode("utf-8")).digest()).decode()
        assert f"'sha256-{h}'" in csp
    for directive in (
        "default-src 'self'",
        "frame-ancestors 'none'",
        "object-src 'none'",
        "base-uri 'none'",
        "connect-src 'self'",
    ):
        assert directive in csp, directive
    assert "'unsafe-inline'" not in csp.split("script-src", 1)[1].split(";", 1)[0]


def test_the_ui_has_no_inline_event_handlers_the_csp_would_block():
    with open(os.path.join(ROOT, "web", "index.html"), encoding="utf-8") as f:
        html = f.read()
    assert not re.search(r"\son[a-z]+\s*=\s*[\"']", html), "inline on*= handler"
    assert "javascript:" not in html


def test_api_responses_have_no_csp_but_keep_their_own_headers(client):
    r = client.get("/health")
    assert "content-security-policy" not in r.headers
    assert r.headers.get("api-version") == "v1"
