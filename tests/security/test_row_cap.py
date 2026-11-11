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
