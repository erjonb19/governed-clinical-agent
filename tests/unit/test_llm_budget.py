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


def test_the_fallback_is_priced_with_its_own_output_cap():
    from nl_to_sql_planner import PROVIDERS, rate_for

    gemini = SimpleNamespace(
        provider="gemini",
        _model="gemini-flash-lite-latest",
        _max_tokens=800,
        fallback_provider="anthropic",
    )
    fb, out = PROVIDERS["anthropic"]["model"], PROVIDERS["anthropic"]["max_tokens"]
    r_fb, r_g = rate_for("anthropic", fb), rate_for("gemini", "gemini-flash-lite-latest")
    per_fb = (4000 * r_fb["input"] + out * r_fb["output"]) / 1e6  # whatever ANTHROPIC_MODEL is
    per_g = (4000 * r_g["input"] + 800 * r_g["output"]) / 1e6
    assert worst_case_usd(gemini, 3) == pytest.approx(6 * max(per_fb, per_g))


# ---- item 19, part 2: the count outlives the process ----------------------


def _stored(tmp_path, now, **caps):
    import state_db

    path = str(tmp_path / "budget.sqlite")
    if state_db.is_postgres():
        path = None  # the Postgres CI job: STATE_DB_URL is the store
    return DailyBudget(now=now, store_path=path, **caps)


def test_the_count_survives_a_restart(tmp_path):
    _, now = _clock(datetime(2026, 11, 24, 12, tzinfo=timezone.utc))
    first = _stored(tmp_path, now, max_runs=2, max_usd=100)
    first.reserve(0.0)
    first.reserve(0.0)
    restarted = _stored(tmp_path, now, max_runs=2, max_usd=100)  # a redeploy
    with pytest.raises(BudgetExceeded):
        restarted.reserve(0.0)
    assert restarted.status()["runs"] == 2


def test_the_stored_cost_cap_and_settle(tmp_path):
    _, now = _clock(datetime(2026, 11, 24, 12, tzinfo=timezone.utc))
    b = _stored(tmp_path, now, max_runs=100, max_usd=1.0)
    r = b.reserve(0.6)
    with pytest.raises(BudgetExceeded) as e:
        b.reserve(0.6)
    assert "$1.00" in e.value.reason
    b.settle(r, 0.01)
    assert b.status()["est_usd"] == pytest.approx(0.01)
    r = b.reserve(0.5)
    b.settle(r, 0.9)  # cost more than the estimate: charged in full
    assert b.status()["est_usd"] == pytest.approx(0.91)


def test_the_stored_day_rolls_over_at_utc_midnight(tmp_path):
    t, now = _clock(datetime(2026, 11, 24, 23, 59, tzinfo=timezone.utc))
    b = _stored(tmp_path, now, max_runs=1, max_usd=100)
    b.reserve(0.0)
    with pytest.raises(BudgetExceeded):
        b.reserve(0.0)
    t[0] = datetime(2026, 11, 25, 0, 0, 1, tzinfo=timezone.utc)
    b.reserve(0.0)
    assert b.status()["runs"] == 1


def test_concurrent_runs_never_pass_the_cap(tmp_path):
    import threading

    _, now = _clock(datetime(2026, 11, 24, 12, tzinfo=timezone.utc))
    path = None
    import state_db

    if not state_db.is_postgres():
        path = str(tmp_path / "budget.sqlite")
        DailyBudget(now=now, store_path=path).status()  # create the table first
    ok, refused = [], []

    def run():
        # each thread is its own "process": a separate budget on one store
        b = DailyBudget(now=now, store_path=path, max_runs=5, max_usd=100)
        try:
            b.reserve(0.0)
            ok.append(1)
        except BudgetExceeded:
            refused.append(1)

    threads = [threading.Thread(target=run) for _ in range(20)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert len(ok) == 5 and len(refused) == 15


def test_an_unreachable_store_refuses_the_run(tmp_path, monkeypatch):
    _, now = _clock(datetime(2026, 11, 24, 12, tzinfo=timezone.utc))
    b = DailyBudget(now=now, store_path=str(tmp_path / "budget.sqlite"))

    def down(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(llm_budget._Store, "charge", down)
    with pytest.raises(BudgetExceeded) as e:
        b.reserve(0.0)
    assert "could not be checked" in e.value.reason


def test_without_state_db_url_the_count_stays_in_memory(monkeypatch):
    monkeypatch.delenv("STATE_DB_URL", raising=False)
    b = DailyBudget(max_runs=3, max_usd=1)
    assert b._store() is None


def test_an_unreachable_budget_store_is_503_not_429(client, monkeypatch):
    def down(*a, **k):
        raise BudgetExceeded("the model budget could not be checked", 60, unavailable=True)

    monkeypatch.setattr(llm_budget.BUDGET, "reserve", down)
    r = client.post(
        "/query", json={"question": "how many hospitals?"}, headers={"X-API-Key": "test-key"}
    )
    assert r.status_code == 503 and "tomorrow" not in r.text
    assert r.headers["Retry-After"] == "60"


def test_a_run_straddling_midnight_settles_against_its_own_day(tmp_path):
    t, now = _clock(datetime(2026, 11, 24, 23, 59, 59, tzinfo=timezone.utc))
    b = _stored(tmp_path, now, max_runs=100, max_usd=10)
    late = b.reserve(0.5)
    t[0] = datetime(2026, 11, 25, 0, 0, 1, tzinfo=timezone.utc)
    b.reserve(0.5)
    b.settle(late, 0.0)  # yesterday's run refunds yesterday, not today
    assert b.status()["est_usd"] == pytest.approx(0.5)


def test_a_run_straddling_midnight_in_memory(tmp_path):
    t, now = _clock(datetime(2026, 11, 24, 23, 59, 59, tzinfo=timezone.utc))
    b = DailyBudget(max_runs=100, max_usd=10, now=now)
    late = b.reserve(0.5)
    t[0] = datetime(2026, 11, 25, 0, 0, 1, tzinfo=timezone.utc)
    b.reserve(0.5)
    b.settle(late, 0.0)
    assert b.status()["est_usd"] == pytest.approx(0.5)


def test_a_dead_database_url_refuses_with_503_semantics(monkeypatch):
    # a real connection failure, not a monkeypatched store: port 1 refuses
    monkeypatch.setenv("STATE_DB_URL", "postgresql://u:p@127.0.0.1:1/budget_test")
    b = DailyBudget(max_runs=5, max_usd=1)
    with pytest.raises(BudgetExceeded) as e:
        b.reserve(0.0)
    assert e.value.unavailable and b.status()["runs"] == -1
