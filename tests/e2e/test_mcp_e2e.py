"""MCP end to end over every transport (PLAN v2 7b).

The same assertions run against three real servers, each in its own process:

    stdio            python mcp_server.py
    http standalone  python mcp_server.py --transport http
    http mounted     uvicorn app:app, the tools at /mcp

  - list_tools includes query_analytics and analytics_schema
  - an allowed query returns rows
  - a blocked query (a catalog probe) returns the guard's refusal
  - HTTP only: 401 without a key; Bearer and X-API-Key both work

No LLM anywhere: over MCP the client writes the SQL and the guard rules on it.
Run with RUN_E2E=1 (tests/e2e/conftest.py).
"""

import json
import os
import socket
import subprocess
import sys
import time

import httpx
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client

pytestmark = [pytest.mark.e2e, pytest.mark.anyio]

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
KEY = "e2e-mcp-key"
ALLOWED = "SELECT COUNT(*) AS n FROM gold_hospital_profile"
BLOCKED = "SELECT * FROM information_schema.tables"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(args, env_extra, health_url, log_path):
    """Start a server; its output goes to a file (a pipe nobody drains can fill
    and block it) and is shown if it fails to come up."""
    env = {**os.environ, **env_extra}
    log = open(log_path, "w")
    proc = subprocess.Popen(args, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    deadline = time.time() + 90
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"server exited early:\n{open(log_path).read()}")
        try:
            if httpx.get(health_url, timeout=2).status_code == 200:
                return proc
        except httpx.HTTPError:
            time.sleep(0.5)
    _stop(proc)
    raise RuntimeError(f"server never became healthy:\n{open(log_path).read()}")


def _stop(proc):
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


# Function-scoped: the API and the MCP server each open the hospital Gold
# read-write, and DuckDB lets only one PROCESS do that. One server at a time.
@pytest.fixture
def standalone(tmp_path):
    port = _free_port()
    proc = _serve([sys.executable, "mcp_server.py", "--transport", "http", "--port", str(port)],
                  {"MCP_API_KEY": KEY}, f"http://127.0.0.1:{port}/health", tmp_path / "server.log")
    yield f"http://127.0.0.1:{port}/mcp"
    _stop(proc)


@pytest.fixture
def mounted(tmp_path):
    port = _free_port()
    tmp = tmp_path
    proc = _serve([sys.executable, "-m", "uvicorn", "app:app", "--port", str(port)],
                  {"API_KEY": "e2e-api-key", "MCP_API_KEY": KEY,
                   "TRACE_DB": str(tmp / "t.sqlite"), "CHECKPOINT_DB": str(tmp / "c.sqlite"),
                   "APPROVALS_DB": str(tmp / "a.sqlite"), "AUDIT_LOG_DIR": str(tmp / "audit"),
                   "PLANNER_METRICS_LOG": str(tmp / "m.jsonl")},
                  f"http://127.0.0.1:{port}/health", tmp / "server.log")
    yield f"http://127.0.0.1:{port}/mcp"
    _stop(proc)


async def _exercise(session: ClientSession) -> None:
    await session.initialize()
    names = {t.name for t in (await session.list_tools()).tools}
    assert {"query_analytics", "analytics_schema"} <= names

    ok = await session.call_tool("query_analytics", {"sql": ALLOWED})
    body = json.loads(ok.content[0].text)
    assert body["rows"][0]["n"] > 0

    bad = await session.call_tool("query_analytics", {"sql": BLOCKED})
    text = bad.content[0].text
    assert text.startswith("REFUSED") and "catalog" in text

    # the PHI leak the adversarial suite found, refused on every transport
    leak = await session.call_tool("query_analytics", {
        "sql": "SELECT patient_id, birth_date, postal_code FROM gold_patient", "dataset": "fhir"})
    assert leak.content[0].text.startswith("REFUSED") and "PHI rule" in leak.content[0].text


async def test_stdio():
    params = StdioServerParameters(command=sys.executable, args=["mcp_server.py"], cwd=ROOT,
                                   env={**os.environ})
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await _exercise(s)


@pytest.mark.parametrize("server", ["standalone", "mounted"])
@pytest.mark.parametrize("header", [{"Authorization": f"Bearer {KEY}"}, {"X-API-Key": KEY}],
                         ids=["bearer", "x-api-key"])
async def test_http(request, server, header):
    url = request.getfixturevalue(server)
    async with streamablehttp_client(url, headers=header) as (r, w, _):
        async with ClientSession(r, w) as s:
            await _exercise(s)


@pytest.mark.parametrize("server", ["standalone", "mounted"])
def test_http_without_a_key_is_401(request, server):
    url = request.getfixturevalue(server)
    r = httpx.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                   headers={"Accept": "application/json, text/event-stream"})
    assert r.status_code == 401
    r = httpx.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                   headers={"Accept": "application/json, text/event-stream",
                            "Authorization": "Bearer wrong"})
    assert r.status_code == 401
