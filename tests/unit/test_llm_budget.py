"""Backlog item 37: a daily budget for runs that call the model, checked
against the WORST case before each run (the scheduled_runs.py pattern)."""

import os
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
import llm_budget  # noqa: E402
from llm_budget import BudgetExceeded, DailyBudget, worst_case_usd  # noqa: E402


def _clock(start):
    t = [start]
    return t, (lambda: t[0])


def test_the_run_cap_holds_and_resets_at_utc_midnight():
    t, now = _clock(datetime(2026, 11, 11, 23, 0, tzinfo=timezone.utc))
    b = DailyBudget(max_runs=2, max_usd=100, now=now)
    b.reserve(0.0)
    b.reserve(0.0)
    with pytest.raises(BudgetExceeded) as e:
        b.reserve(0.0)
    assert "2 model runs" in e.value.reason and 3500 <= e.value.retry_after <= 3601
    t[0] = datetime(2026, 11, 12, 0, 0, 1, tzinfo=timezone.utc)
    b.reserve(0.0)  # a new day
    assert b.status()["runs"] == 1


def test_the_cost_cap_is_checked_against_the_worst_case_before_the_run():
    _, now = _clock(datetime(2026, 11, 11, 12, tzinfo=timezone.utc))
    b = DailyBudget(max_runs=100, max_usd=1.0, now=now)
    b.reserve(0.6)
    with pytest.raises(BudgetExceeded):
        b.reserve(0.6)  # 1.2 worst case would break the cap: refused before it runs
    assert b.status()["est_usd"] == 0.6


def test_settling_charges_the_actual_cost():
    _, now = _clock(datetime(2026, 11, 11, 12, tzinfo=timezone.utc))
    b = DailyBudget(max_runs=100, max_usd=1.0, now=now)
    r = b.reserve(0.6)
    b.settle(r, 0.01)
    assert b.status()["est_usd"] == 0.01
    r = b.reserve(0.3)
    b.settle(r, None)  # nothing reported: the worst case stays charged
    assert b.status()["est_usd"] == pytest.approx(0.31)
    r = b.reserve(0.5)
    b.settle(r, 0.9)  # cost more than the estimate: charged in full, never undercounted
    assert b.status()["est_usd"] == pytest.approx(1.21)
    with pytest.raises(BudgetExceeded):
        b.reserve(0.0001)  # and the next run sees it


def test_worst_case_covers_every_attempt_plan_and_critic():
    p = SimpleNamespace(provider="anthropic", _model="claude-haiku-5-5", _max_tokens=4000)
    # haiku: $0.10 in, $0.50 out per 1M; 3 attempts x 2 calls x (4000 in + 4000 out)
    assert worst_case_usd(p, 3) == pytest.approx(6 * (4000 * 0.10 + 4000 * 0.50) / 1e6)
    assert worst_case_usd(SimpleNamespace(provider="replay", _model="x"), 3) == 0.0
    assert worst_case_usd(None, 3) == 0.0


def test_worst_case_prices_at_the_dearer_of_primary_and_fallback():
    gemini = SimpleNamespace(provider="gemini", _model="gemini-flash-lite-latest", _max_tokens=800)
    alone = worst_case_usd(gemini, 3)
    with_groq = worst_case_usd(SimpleNamespace(**vars(gemini), fallback_provider="groq"), 3)
    # groq ($0.59 / $0.79) is dearer than gemini ($0.15 / $0.60)
    assert with_groq == pytest.approx(6 * (4000 * 0.59 + 800 * 0.79) / 1e6)
    assert with_groq > alone


@pytest.fixture
def client(tmp_path, monkeypatch):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    for k, v in {
        "API_KEY": "test-key",
        "TRACE_DB": tmp_path / "t.sqlite",
        "CHECKPOINT_DB": tmp_path / "c.sqlite",
        "APPROVALS_DB": tmp_path / "a.sqlite",
        "AUDIT_LOG_DIR": tmp_path / "audit",
        "PLANNER_METRICS_LOG": tmp_path / "m.jsonl",
        "RATE_LIMIT_QUERY": "100000/minute",
        "RATE_LIMIT_READ": "100000/minute",
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


def test_query_past_the_daily_cap_is_429_and_raw_sql_is_not_held_back(client):
    llm_budget.BUDGET.max_runs = 0
    r = client.post(
        "/query", json={"question": "how many hospitals?"}, headers={"X-API-Key": "test-key"}
    )
    assert r.status_code == 429 and "daily limit" in r.text, r.text
    assert int(r.headers["Retry-After"]) >= 1
    q = client.post(
        "/raw-sql",
        json={"sql": "SELECT count(*) AS n FROM gold_hospital_profile"},
        headers={"X-API-Key": "test-key"},
    )
    assert q.status_code == 200  # a guarded query that calls no model is not budgeted
    m = client.get("/metrics", headers={"X-API-Key": "test-key"})
    assert m.status_code == 200 and m.json()["llm_budget"]["max_runs"] == 0
