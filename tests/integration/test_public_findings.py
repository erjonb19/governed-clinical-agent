"""The keyless findings route (backlog item 9, owner's decision D1a, 2026-11-14).

The owner's conditions, each pinned here: hospital data only, only the fixed
reviewed findings queries, no LLM calls, rate-limited, no key exposed to the
browser. Plus: every other data route still refuses a request with no key.
"""

import importlib
import os
import sys

import pytest

pytest.importorskip("httpx")
from fastapi.routing import APIRoute  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

GOLD = os.path.join(os.path.dirname(__file__), "..", "..", "medallion", "hospital_gold.duckdb")
KEY = "test-key"

# Every route that answers with no key. A new one fails the test below until it
# is added here, which forces a decision about it.
OPEN_ROUTES = {
    "/",
    "/ui",
    "/ui-config",
    "/health",
    "/datasets",
    "/metrics",
    "/docs",
    "/openapi.json",
    "/docs/oauth2-redirect",
    "/redoc",
    "/public/findings",
    "/schema",  # table and column names only; open before item 9, unchanged here
}


def _make(tmp_path, monkeypatch, **env):
    if not os.path.exists(GOLD):
        pytest.skip("hospital Gold not built")
    base = {
        "API_KEY": KEY,
        "TRACE_DB": tmp_path / "t.sqlite",
        "CHECKPOINT_DB": tmp_path / "c.sqlite",
        "APPROVALS_DB": tmp_path / "a.sqlite",
        "AUDIT_LOG_DIR": tmp_path / "audit",
        "PLANNER_METRICS_LOG": tmp_path / "m.jsonl",
    }
    for k, v in {**base, **env}.items():
        monkeypatch.setenv(k, str(v))
    import src.tools.base as tool_base

    saved = dict(tool_base._TOOL_REGISTRY)
    tool_base._TOOL_REGISTRY.pop("analytics.query_aggregate", None)
    import tracing

    tracing.set_tracer(None)
    importlib.reload(tracing)
    import app as app_module

    importlib.reload(app_module)
    return app_module, saved


@pytest.fixture
def make(tmp_path, monkeypatch):
    made = []

    def factory(**env):
        app_module, saved = _make(tmp_path, monkeypatch, **env)
        made.append(saved)
        return app_module

    yield factory
    import src.tools.base as tool_base
    import tracing

    tracing.set_tracer(None)
    for saved in made:
        tool_base._TOOL_REGISTRY.clear()
        tool_base._TOOL_REGISTRY.update(saved)


def test_answers_with_no_key_and_matches_the_keyed_findings(make):
    app_module = make()
    with TestClient(app_module.app) as c:
        r = c.get("/public/findings")
        assert r.status_code == 200
        body = r.json()
        assert body["dataset"] == "hospital" and body["backend"] == "local"
        assert len(body["findings"]) >= 3
        assert all(f["status"] == "ok" for f in body["findings"])
        assert body == c.get("/findings", headers={"X-API-Key": KEY}).json()
        assert c.get("/v1/public/findings").json() == body


def test_takes_no_input_that_could_reach_sql_or_another_dataset(make):
    app_module = make()
    with TestClient(app_module.app) as c:
        plain = c.get("/public/findings").json()
        assert (
            c.get("/public/findings", params={"dataset": "fhir", "sql": "SELECT 1"}).json() == plain
        )
        assert c.post("/public/findings", json={"sql": "SELECT 1"}).status_code == 405
    route = next(
        r for r in app_module.app.routes if isinstance(r, APIRoute) and r.path == "/public/findings"
    )
    params = {
        p.name
        for p in route.dependant.query_params
        + route.dependant.body_params
        + route.dependant.path_params
    }
    assert params == set()


def test_makes_no_model_call(make, monkeypatch):
    app_module = make()
    import findings

    calls = []
    monkeypatch.setattr(
        app_module.llm_budget.BUDGET,
        "reserve",
        lambda *a, **k: (
            calls.append("reserve") or (_ for _ in ()).throw(AssertionError("model budget used"))
        ),
    )
    if app_module._STATE.get("planner") is not None:
        monkeypatch.setattr(
            app_module._STATE["planner"],
            "plan",
            lambda *a, **k: (
                calls.append("plan") or (_ for _ in ()).throw(AssertionError("planner called"))
            ),
        )
    app_module._STATE.pop("findings_cache", None)  # force a real computation
    with TestClient(app_module.app) as c:
        app_module._STATE.pop("findings_cache", None)
        body = c.get("/public/findings").json()
    assert calls == [] and len(body["findings"]) == len(findings.FINDINGS)


def test_returns_no_key_or_secret(make):
    app_module = make()
    with TestClient(app_module.app) as c:
        r = c.get("/public/findings")
    assert KEY not in r.text
    assert "key" not in {k.lower() for k in r.json()}


def test_rate_limited_per_address_and_a_made_up_key_does_not_reset_it(make):
    app_module = make(RATE_LIMIT_PUBLIC="2/minute")
    with TestClient(app_module.app) as c:
        assert c.get("/public/findings").status_code == 200
        assert c.get("/public/findings").status_code == 200
        assert c.get("/public/findings").status_code == 429
        assert c.get("/public/findings", headers={"X-API-Key": "made-up-1"}).status_code == 429
        assert c.get("/public/findings", headers={"X-API-Key": KEY}).status_code == 429


def test_every_other_route_still_needs_the_key(make):
    app_module = make()
    paths = {r.path for r in app_module.app.routes if isinstance(r, APIRoute)}
    with TestClient(app_module.app) as c:
        for route in app_module.app.routes:
            if not isinstance(route, APIRoute) or route.path in OPEN_ROUTES:
                continue
            path = route.path.replace("{", "").replace("}", "")  # any placeholder value
            method = sorted(route.methods - {"HEAD", "OPTIONS"})[0]
            r = c.request(method, path, json={})
            assert r.status_code == 401, (method, route.path, r.status_code)
    assert OPEN_ROUTES - {"/docs/oauth2-redirect", "/redoc", "/docs", "/openapi.json"} <= paths | {
        "/"
    }
