"""Backlog item 43: the row cap holds on a UNION / set operation too.

The guard adds or clamps a LIMIT on a plain SELECT, but a set operation's top
node is not a SELECT, so no LIMIT was added and fetchall() pulled the whole
result into memory before it was sliced. Now the fetch stops one row past the
cap and a longer result is marked truncated."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import analytics_query_tool as aqt  # noqa: E402
from analytics_query_tool import AnalyticsQueryTool  # noqa: E402

UNION = (
    "SELECT * FROM (SELECT 1 AS n FROM range(5000)) "
    "UNION ALL SELECT * FROM (SELECT 2 AS n FROM range(5000))"
)


def _tool(cap):
    gold = os.path.join(os.path.dirname(__file__), "..", "..", "medallion", "hospital_gold.duckdb")
    return AnalyticsQueryTool(
        db_path=gold if os.path.exists(gold) else ":memory:",
        seed_demo=not os.path.exists(gold),
        row_cap=cap,
    )


def test_a_union_returns_at_most_the_cap_and_says_it_was_cut():
    r = _tool(50).execute({"sql": UNION})
    assert r.success, r.error
    assert len(r.output["rows"]) == 50 and r.output["row_count"] == 50
    assert r.output["truncated"] is True and r.output["row_cap"] == 50


def test_the_fetch_stops_one_row_past_the_cap(monkeypatch):
    fetched = {}
    real = aqt.run_with_timeout

    def spy(con, sql, seconds, max_rows=None):
        cols, rows = real(con, sql, seconds, max_rows=max_rows)
        fetched["n"], fetched["max_rows"] = len(rows), max_rows
        return cols, rows

    monkeypatch.setattr(aqt, "run_with_timeout", spy)
    assert _tool(50).execute({"sql": UNION}).success
    assert fetched == {"n": 51, "max_rows": 50}, fetched


def test_a_result_under_the_cap_is_not_marked():
    r = _tool(50).execute({"sql": "SELECT 1 AS n UNION ALL SELECT 2 AS n"})
    assert r.success and len(r.output["rows"]) == 2 and "truncated" not in r.output


def test_raw_sql_says_truncated(tmp_path, monkeypatch):
    import pytest

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
    try:
        with TestClient(app_module.app) as c:
            body = c.post("/raw-sql", json={"sql": UNION}, headers={"X-API-Key": "test-key"}).json()
        assert body["allowed"] and body["truncated"] is True
        assert len(body["rows"]) == body["row_count"] == 1000, body["row_count"]
    finally:
        tracing.set_tracer(None)
        tool_base._TOOL_REGISTRY.pop("analytics.query_aggregate", None)
        if previous is not None:
            tool_base._TOOL_REGISTRY["analytics.query_aggregate"] = previous
