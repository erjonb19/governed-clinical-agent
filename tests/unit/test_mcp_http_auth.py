"""MCP over HTTP: the API-key and Origin checks in front of the MCP app (PLAN v2 item 5).

The middleware is exercised through a tiny Starlette app, so this needs no MCP
session: what is under test is who gets through to the endpoint at all.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

pytest.importorskip("mcp", reason="the MCP front door needs the mcp package")
pytest.importorskip("httpx")
from starlette.applications import Starlette  # noqa: E402
from starlette.routing import Route  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

import mcp_server as M  # noqa: E402


async def _ok(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"through"})


def client(key, origins=None):
    app = Starlette(routes=[Route("/mcp", endpoint=M.MCPAuth(_ok, key, origins or []),
                                  methods=["POST"])])
    return TestClient(app)


def test_no_key_and_a_wrong_key_are_401():
    c = client("s3cret")
    r = c.post("/mcp")
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
    assert c.post("/mcp", headers={"X-API-Key": "nope"}).status_code == 401
    assert c.post("/mcp", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert c.post("/mcp", headers={"Authorization": "Basic s3cret"}).status_code == 401


def test_both_header_forms_get_through():
    c = client("s3cret")
    assert c.post("/mcp", headers={"X-API-Key": "s3cret"}).text == "through"
    assert c.post("/mcp", headers={"Authorization": "Bearer s3cret"}).text == "through"
    assert c.post("/mcp", headers={"authorization": "bearer s3cret"}).text == "through"


def test_a_foreign_browser_origin_is_refused_even_with_the_key():
    c = client("s3cret")
    r = c.post("/mcp", headers={"X-API-Key": "s3cret", "Origin": "https://evil.example"})
    assert r.status_code == 403


def test_local_and_listed_origins_are_allowed():
    c = client("s3cret", ["https://claude.ai"])
    for origin in ("http://localhost:6274", "http://127.0.0.1", "https://claude.ai"):
        r = c.post("/mcp", headers={"X-API-Key": "s3cret", "Origin": origin})
        assert r.status_code == 200, origin
    # a lookalike is not a prefix match
    assert not M.origin_allowed("http://localhost.evil.example", [])


def test_open_access_lets_requests_through_but_still_checks_origin():
    c = client(None)
    assert c.post("/mcp").text == "through"
    assert c.post("/mcp", headers={"Origin": "https://evil.example"}).status_code == 403


def test_the_key_falls_back_from_mcp_api_key_to_api_key(monkeypatch):
    monkeypatch.delenv("MCP_API_KEY", raising=False)
    monkeypatch.setenv("API_KEY", "api")
    assert M.mcp_key() == "api"
    monkeypatch.setenv("MCP_API_KEY", "mcp")
    assert M.mcp_key() == "mcp"


def test_http_refuses_to_start_without_a_key(monkeypatch):
    monkeypatch.delenv("MCP_API_KEY", raising=False)
    monkeypatch.delenv("API_KEY", raising=False)
    monkeypatch.delenv("ALLOW_OPEN_ACCESS", raising=False)
    with pytest.raises(SystemExit, match="REFUSING TO START"):
        M.resolve_http_key()
    monkeypatch.setenv("ALLOW_OPEN_ACCESS", "true")
    assert M.resolve_http_key() is None


def test_registration_reuses_a_capability_that_is_already_registered():
    # mounted in the API, analytics.query_aggregate is already registered; the
    # MCP server must reuse it rather than raise
    import src.tools.base as tool_base
    existing = tool_base.get_tool(M.EchoTool().name)
    if existing is None:
        M.runtime.register_tool(M.EchoTool())
    M._ensure_registered(M.runtime, M.EchoTool())   # no ValueError


def test_requests_past_the_limit_get_429():
    clock = [0.0]
    limit = M.RateLimit(per_minute=3, clock=lambda: clock[0])
    app = Starlette(routes=[Route("/mcp", endpoint=M.MCPAuth(_ok, "k", [], limit), methods=["POST"])])
    c = TestClient(app)
    codes = [c.post("/mcp", headers={"X-API-Key": "k"}).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]
    clock[0] = 61.0                      # a new window
    assert c.post("/mcp", headers={"X-API-Key": "k"}).status_code == 200


def test_a_rejected_key_does_not_spend_the_limit():
    limit = M.RateLimit(per_minute=1)
    app = Starlette(routes=[Route("/mcp", endpoint=M.MCPAuth(_ok, "k", [], limit), methods=["POST"])])
    c = TestClient(app)
    for _ in range(5):
        assert c.post("/mcp", headers={"X-API-Key": "bad"}).status_code == 401
    assert c.post("/mcp", headers={"X-API-Key": "k"}).status_code == 200


def test_a_websocket_is_refused_not_passed_through():
    import asyncio
    sent, reached = [], []

    async def app(scope, receive, send):
        reached.append(1)

    async def send(msg):
        sent.append(msg)

    asyncio.run(M.MCPAuth(app, "k", [])({"type": "websocket", "headers": []}, None, send))
    assert reached == [] and sent == [{"type": "websocket.close", "code": 1008}]


def test_mounted_calls_use_the_host_lock(monkeypatch):
    # the API's DuckDB connection is not thread-safe; mounted, every tool call
    # must hold the API's lock
    import threading
    lock = threading.Lock()
    seen = []

    class Rt:
        audit_logger = None

        def execute_tool(self, cap, params):
            seen.append(lock.locked())
            from types import SimpleNamespace
            return SimpleNamespace(allowed=True, result=SimpleNamespace(success=True, output="x",
                                                                         error=None))

    monkeypatch.setattr(M, "runtime", Rt())
    M.attach_host(audit_logger="AUDIT", lock=lock)
    try:
        assert M.call_guarded("demo.echo", {"text": "hi"})[0] is True
        assert seen == [True] and M.runtime.audit_logger == "AUDIT"
    finally:
        M._EXEC_LOCK = None
