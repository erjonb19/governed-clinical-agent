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


# --- each number checked against an independent computation ---------------------
# Not the finding's own SQL: plain rows from the Gold, summarised in Python, so a
# flipped comparison (mspb_score > 1.0, >= 5) or a wrong column fails here.

def _gold_rows(cols):
    import duckdb
    con = duckdb.connect(GOLD, read_only=True)
    try:
        return con.execute(f"SELECT {', '.join(cols)} FROM gold_hospital_profile").fetchall()
    finally:
        con.close()


def _card(client, fid):
    c, _ = client
    return next(f for f in c.get("/findings", headers=H).json()["findings"] if f["id"] == fid)


def test_hf_names_the_state_with_the_highest_unrounded_average(client):
    by_state = {}
    for state, v in _gold_rows(["state", "readmit_hf"]):
        if v is not None:
            by_state.setdefault(state, []).append(v)
    avgs = {s: sum(v) / len(v) for s, v in by_state.items() if len(v) >= findings.MIN_HOSPITALS}
    worst, best = max(avgs, key=avgs.get), min(avgs, key=avgs.get)
    f = _card(client, "hf_readmission_by_state")
    assert f["headline"].startswith(worst) and f["metric"] == f"{avgs[worst]:.1f}%"
    assert f"{best} has the lowest" in f["sentence"]


def test_high_value_counts_4_or_5_stars_below_average_cost(client):
    rows = _gold_rows(["star_rating", "mspb_score"])
    rated = [(s, m) for s, m in rows if s is not None and m is not None]
    hv = sum(1 for s, m in rated if s >= 4 and m < 1.0)
    f = _card(client, "high_quality_low_cost")
    assert f["sentence"].startswith(f"{hv} of the {len(rated)} hospitals")


def test_psych_names_the_slowest_state_with_enough_hospitals(client):
    import statistics
    by_state, everyone = {}, []
    for state, v in _gold_rows(["state", "ed_psych_median_min"]):
        if v is not None:
            by_state.setdefault(state, []).append(v)
            everyone.append(v)
    meds = {s: statistics.median(v) for s, v in by_state.items() if len(v) >= findings.MIN_HOSPITALS}
    top = max(meds, key=meds.get)
    f = _card(client, "psych_ed_wait")
    assert f"wait longest in {top}," in f["headline"]
    assert abs(float(f["metric"].split()[0]) - meds[top]) <= 0.5
    overall = float(re.search(r"against (\d+) minutes", f["sentence"]).group(1))
    assert abs(overall - statistics.median(everyone)) <= 0.5


def test_left_before_seen_counts_more_than_5_percent(client):
    vals = [v for (v,) in _gold_rows(["ed_left_before_seen_pct"]) if v is not None]
    f = _card(client, "ed_left_before_seen")
    assert f["sentence"].startswith(f"{sum(1 for v in vals if v > 5)} of the {len(vals)} hospitals")


def test_a_set_with_an_unavailable_finding_is_not_cached(client, monkeypatch):
    c, _ = client
    calls = []
    real = findings.compute

    def flaky(run_sql, vintage):
        calls.append(1)
        cards = real(run_sql, vintage)
        if len(calls) == 1:
            cards[0] = {**cards[0], "status": "unavailable", "reason": "transient"}
        return cards
    monkeypatch.setattr(findings, "compute", flaky)
    first = c.get("/findings", headers=H).json()
    assert first["findings"][0]["status"] == "unavailable"
    second = c.get("/findings", headers=H).json()
    assert calls == [1, 1] and second["findings"][0]["status"] == "ok"   # retried, not cached
    c.get("/findings", headers=H)
    assert calls == [1, 1]                                               # now cached


@pytest.mark.parametrize("fid,rows", [
    ("high_quality_low_cost", [[{"high_value": 0, "rated": 0}]]),
    ("ed_left_before_seen", [[{"over_5": 0, "reporting": 0}]]),
    ("psych_ed_wait", [[{"state": "MA", "median_min": 300, "hospitals": 12}], [{"median_min": None}]]),
])
def test_an_empty_denominator_is_unavailable_not_a_number(fid, rows):
    f = next(x for x in findings.FINDINGS if x["id"] == fid)
    with pytest.raises(ValueError):
        f["render"](rows)
