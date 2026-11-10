"""Backlog item 20, part 2: queries run side by side, and stay governed.

The API used to serialise every query on one global lock. Now the query tool
opens a DuckDB cursor of its own per execution, so these hold under real
concurrency:

- every request gets ITS OWN guard decision (no answer crosses to another
  request), and the audit log has exactly one policy line and one execution
  line per request, with the right outcome;
- a query stopped by the timeout stops alone: a fast query running at the same
  time still answers;
- runs that call the model are bounded (MAX_CONCURRENT_RUNS); past the queue
  wait the caller gets 429 with Retry-After.
"""

import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

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
        "RATE_LIMIT_QUERY": "100000/minute",
        "RATE_LIMIT_READ": "100000/minute",
        "MAX_CONCURRENT_RUNS": "1",
        "RUN_QUEUE_WAIT_SEC": "0",
    }.items():
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


def _requests(n):
    """Half allowed (each returns its own marker k), half refused by the guard
    (each names its own table), interleaved."""
    out = []
    for i in range(n):
        if i % 2 == 0:
            out.append((i, f"SELECT count(*) AS n, {i} AS k FROM gold_hospital_profile", True))
        else:
            out.append((i, f"SELECT * FROM bronze_t{i}", False))
    return out


def test_parallel_queries_each_get_their_own_decision_and_audit_lines(client, tmp_path):
    c, _ = client
    reqs = _requests(48)
    start = threading.Barrier(8)

    def call(item):
        i, sql, _ = item
        if i < 8:
            start.wait()  # the first 8 go at once
        return i, c.post("/raw-sql", json={"sql": sql}, headers=H).json()

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = dict(pool.map(call, reqs))

    for i, sql, allowed in reqs:
        r = results[i]
        assert r["sql"] == sql, (i, r)  # its own answer, not another's
        assert r["allowed"] is allowed, (i, r)
        if allowed:
            assert r["rows"] == [{"n": r["rows"][0]["n"], "k": i}], (i, r)
            assert r["rows"][0]["n"] > 0
        else:
            assert r["decided_by"] == "guard" and f"bronze_t{i}" in r["reason"], (i, r)

    events = [
        json.loads(line)
        for f in (tmp_path / "audit").glob("audit_api_*.jsonl")
        for line in f.read_text().splitlines()
        if line.strip()
    ]
    for i, sql, allowed in reqs:
        mine = [e for e in events if (e.get("parameters") or {}).get("sql") == sql]
        kinds = sorted(e["event_type"] for e in mine)
        assert kinds == ["policy_evaluation", "tool_execution"], (i, kinds)
        execution = next(e for e in mine if e["event_type"] == "tool_execution")
        assert execution["decision"] == ("allow" if allowed else "deny"), (i, execution)
        assert "rows" not in json.dumps(execution.get("result") or {}) or allowed


def test_a_timed_out_query_stops_alone(monkeypatch, tmp_path):
    monkeypatch.setenv("QUERY_TIMEOUT_SEC", "1")
    from analytics_query_tool import AnalyticsQueryTool

    gold = os.path.join(ROOT, "medallion", "hospital_gold.duckdb")
    tool = AnalyticsQueryTool(db_path=gold, seed_demo=False)
    slow = "SELECT count(*) AS n FROM range(200000000) a, range(1000) b WHERE a.range + b.range < 0"
    fast = "SELECT count(*) AS n FROM gold_hospital_profile"
    started = threading.Event()
    out = {}

    def run_slow():
        started.set()
        out["slow"] = tool.execute({"sql": slow})

    t = threading.Thread(target=run_slow)
    t.start()
    started.wait()
    fast_results = [tool.execute({"sql": fast}) for _ in range(5)]
    t.join(timeout=30)
    assert not t.is_alive()
    assert all(r.success for r in fast_results), [r.error for r in fast_results]
    assert not out["slow"].success and "timed out" in out["slow"].error, out["slow"].error


def test_model_runs_beyond_the_limit_get_429_with_retry_after(client):
    c, app_module = client
    assert app_module.MAX_CONCURRENT_RUNS == 1
    assert app_module._RUN_SLOTS.acquire(timeout=1)  # someone is being answered
    try:
        r = c.post("/query", json={"question": "how many hospitals?"}, headers=H)
        assert r.status_code == 429, r.text
        assert r.headers.get("Retry-After") == str(app_module.RUN_RETRY_AFTER_SEC)
        # a guarded query that needs no model is not held back
        q = c.post(
            "/raw-sql", json={"sql": "SELECT count(*) AS n FROM gold_hospital_profile"}, headers=H
        )
        assert q.status_code == 200 and q.json()["allowed"] is True
    finally:
        app_module._RUN_SLOTS.release()
    r = c.post("/query", json={"question": "how many hospitals?"}, headers=H)
    assert r.status_code == 200, r.text  # the slot is free again
