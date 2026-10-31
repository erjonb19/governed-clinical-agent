"""The leadership Overview's findings (PLAN v2 item 6): deterministic, guarded, soft-failing.

Against the committed hospital Gold through the real app, so every finding's SQL
really passes sql_guard and really renders. No LLM is involved at any point.
"""

import importlib
import os
import re
import sys

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import findings  # noqa: E402
from sql_guard import validate_query  # noqa: E402

GOLD = os.path.join(os.path.dirname(__file__), "..", "..", "medallion", "hospital_gold.duckdb")
H = {"X-API-Key": "test-key"}


def test_every_finding_query_passes_the_guard():
    for f in findings.FINDINGS:
        for sql in f["sql"]:
            assert validate_query(sql).allowed, (f["id"], sql)


@pytest.fixture
def client(tmp_path, monkeypatch):
    if not os.path.exists(GOLD):
        pytest.skip("hospital Gold not built")
    for k, v in {"API_KEY": "test-key", "TRACE_DB": tmp_path / "t.sqlite",
                 "CHECKPOINT_DB": tmp_path / "c.sqlite", "APPROVALS_DB": tmp_path / "a.sqlite",
                 "AUDIT_LOG_DIR": tmp_path / "audit", "PLANNER_METRICS_LOG": tmp_path / "m.jsonl"}.items():
        monkeypatch.setenv(k, str(v))
    import src.tools.base as tool_base
    saved = dict(tool_base._TOOL_REGISTRY)
    tool_base._TOOL_REGISTRY.pop("analytics.query_aggregate", None)
    import tracing
    tracing.set_tracer(None)
    importlib.reload(tracing)
    import app as app_module
    importlib.reload(app_module)
    try:
        with TestClient(app_module.app) as c:
            yield c, app_module
    finally:
        tracing.set_tracer(None)
        tool_base._TOOL_REGISTRY.clear()
        tool_base._TOOL_REGISTRY.update(saved)


def test_findings_need_the_key(client):
    c, _ = client
    assert c.get("/findings").status_code == 401


def test_every_finding_renders_a_number_from_the_data(client):
    c, _ = client
    body = c.get("/findings", headers=H).json()
    assert body["vintage"] and len(body["findings"]) >= 3
    for f in body["findings"]:
        assert f["status"] == "ok", f
        assert re.search(r"\d", f["metric"]) and re.search(r"\d", f["sentence"])
        assert f["sql"] and f["vintage"] == body["vintage"]


def test_the_state_comparison_names_real_states_with_enough_hospitals(client):
    c, _ = client
    hf = next(f for f in c.get("/findings", headers=H).json()["findings"]
              if f["id"] == "hf_readmission_by_state")
    rows = c.post("/raw-sql", headers=H, json={"sql": findings.FINDINGS[0]["sql"][0]}).json()["rows"]
    assert all(r["hospitals"] >= findings.MIN_HOSPITALS for r in rows)
    assert rows[0]["state"] in hf["sentence"] and rows[-1]["state"] in hf["sentence"]


def test_a_finding_that_cannot_be_computed_shows_no_number():
    def deny(sql):
        return {"allowed": False, "decided_by": "guard", "reason": "DENIED by sql_guard: test"}
    cards = findings.compute(deny, "2026-10-04")
    assert all(c["status"] == "unavailable" for c in cards)
    assert all("metric" not in c and "sentence" not in c for c in cards)
    assert "DENIED" in cards[0]["reason"]


def test_findings_are_cached_per_vintage_and_failures_are_not(client, monkeypatch):
    c, app_module = client
    calls = []
    real = findings.compute

    def counting(run_sql, vintage):
        calls.append(1)
        return real(run_sql, vintage)
    monkeypatch.setattr(findings, "compute", counting)
    c.get("/findings", headers=H)
    c.get("/findings", headers=H)
    assert calls == [1]                                  # served from the cache
    app_module._STATE["findings_cache"] = (("another vintage", 0), {})
    c.get("/findings", headers=H)
    assert calls == [1, 1]                               # a new vintage recomputes
