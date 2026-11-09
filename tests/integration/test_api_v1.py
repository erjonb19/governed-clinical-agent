"""Backlog item 5: /v1 paths, the app version and /health.

Every endpoint answers under /v1 too (one route set, so the two cannot drift),
the old paths keep working, every response says `API-Version: v1`, and /health
reports the version from VERSION and the git commit.
"""

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
    for k, v in {"API_KEY": "test-key", "TRACE_DB": tmp_path / "t.sqlite",
                 "CHECKPOINT_DB": tmp_path / "c.sqlite", "APPROVALS_DB": tmp_path / "a.sqlite",
                 "AUDIT_LOG_DIR": tmp_path / "audit",
                 "PLANNER_METRICS_LOG": tmp_path / "m.jsonl"}.items():
        monkeypatch.setenv(k, str(v))
    import importlib
    import src.tools.base as tool_base
    previous = tool_base._TOOL_REGISTRY.pop("analytics.query_aggregate", None)
    import tracing
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


def test_version_file_is_semver():
    with open(os.path.join(ROOT, "VERSION")) as f:
        assert re.fullmatch(r"\d+\.\d+\.\d+", f.read().strip())


def test_health_reports_version_and_commit(client):
    c, app_module = client
    body = c.get("/health").json()
    assert body["version"] == app_module.APP_VERSION and body["api_version"] == "v1"
    assert body["git_commit"]
    assert c.get("/openapi.json").json()["info"]["version"] == app_module.APP_VERSION


def test_v1_and_the_old_path_give_the_same_answer(client):
    c, _ = client
    old, new = c.get("/health"), c.get("/v1/health")
    assert old.status_code == new.status_code == 200
    assert old.json() == new.json()
    assert new.headers["api-version"] == "v1" and old.headers["api-version"] == "v1"


def test_auth_still_applies_under_v1(client):
    c, _ = client
    # /traces needs the key; /v1/traces exactly the same
    assert c.get("/traces").status_code == c.get("/v1/traces").status_code == 401
    assert c.get("/v1/traces", headers=H).status_code == 200


def test_raw_sql_runs_through_the_guard_under_v1(client):
    c, _ = client
    ok = c.post("/v1/raw-sql", json={"sql": "SELECT count(*) AS n FROM gold_hospital_profile"},
                headers=H).json()
    assert ok["allowed"]
    denied = c.post("/v1/raw-sql", json={"sql": "DROP TABLE gold_hospital_profile"},
                    headers=H).json()
    assert not denied["allowed"]


def test_only_the_exact_prefix_is_stripped(client):
    c, _ = client
    assert c.get("/v1health").status_code == 404      # not a /v1/ path
    assert c.get("/v2/health").status_code == 404     # no other version exists


def test_the_prefix_is_stripped_from_the_raw_bytes_too():
    import asyncio
    import app as app_module
    seen = {}

    async def inner(scope, receive, send):
        seen.update(path=scope["path"], raw=scope["raw_path"])

    mw = app_module._APIVersionPrefix(inner)
    asyncio.run(mw({"type": "http", "path": "/v1/traces/a/b",
                    "raw_path": b"/v1/traces/a%2Fb"}, None, None))
    assert seen == {"path": "/traces/a/b", "raw": b"/traces/a%2Fb"}
