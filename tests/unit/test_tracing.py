"""tracing.py: spans nest, rows are never stored, the store is best effort, and
old runs are pruned.

WHY THIS FILE EXISTS
The trace store is the first place a FHIR result could leak to disk outside the
guarded query path, so the no-rows rule (CLAUDE.md rule 3) is pinned here with
a test rather than a comment. The best-effort contract matters just as much: a
tracer that raises turns a logging problem into a failed request.
"""

import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import tracing


@pytest.fixture
def tracer(tmp_path):
    return tracing.Tracer(str(tmp_path / "traces.sqlite"), max_runs=1000)


def test_spans_nest_under_the_current_span(tracer):
    with tracer.trace("run", question="q") as tid:
        with tracer.span("plan", attempt=1) as plan:
            plan.set(sql="SELECT 1")
            with tracer.span("guard") as guard:
                tracer.event("execute", row_count=3)
        tracer.event("answer", outcome="answered")

    t = tracer.get_trace(tid)
    assert t["status"] == "ok" and t["attrs"] == {"question": "q"}
    by_name = {s["name"]: s for s in t["spans"]}
    assert [s["name"] for s in t["spans"]] == ["plan", "guard", "execute", "answer"]
    assert by_name["plan"]["parent_id"] is None
    assert by_name["guard"]["parent_id"] == by_name["plan"]["span_id"]
    assert by_name["execute"]["parent_id"] == by_name["guard"]["span_id"]
    assert by_name["answer"]["parent_id"] is None
    assert by_name["plan"]["attrs"]["sql"] == "SELECT 1"
    assert tracing.current_trace_id() is None   # context restored


def test_an_exception_marks_the_span_and_trace_and_is_not_swallowed(tracer):
    with pytest.raises(ValueError):
        with tracer.trace("run") as tid:
            with tracer.span("plan"):
                raise ValueError("boom")
    t = tracer.get_trace(tid)
    assert t["status"] == "error" and t["attrs"]["error"].startswith("ValueError")
    assert t["spans"][0]["status"] == "error"


def test_result_rows_are_never_written(tracer):
    rows = [{"patient_id": "p1", "name": "Jane"}]
    with tracer.trace("run", rows=rows, nested={"rows": rows, "row_count": 1}) as tid:
        with tracer.span("execute", rows=rows, result={"rows": rows}, row_count=1) as sp:
            sp.set(rows=rows)
    con = sqlite3.connect(tracer.db_path)
    blobs = [r[0] for r in con.execute("SELECT attrs FROM spans")]
    blobs += [r[0] for r in con.execute("SELECT attrs FROM traces")]
    con.close()
    assert blobs
    for b in blobs:
        assert "Jane" not in b and "p1" not in b
        assert "rows" not in json.loads(b)
    t = tracer.get_trace(tid)
    assert t["spans"][0]["attrs"] == {"row_count": 1}
    assert t["attrs"] == {"nested": {"row_count": 1}}


def test_attach_appends_to_an_existing_trace(tracer):
    tid = tracer.start_trace("approval", trace_id="thread-7", question="q")
    tracer.end_trace(tid, status="pending_approval")
    with tracer.attach("thread-7"):
        tracer.event("approval.decision", decision="approve")
    tracer.end_trace("thread-7", status="ok", committed=True)
    t = tracer.get_trace("thread-7")
    assert t["status"] == "ok"
    assert t["attrs"] == {"question": "q", "committed": True}
    assert [s["name"] for s in t["spans"]] == ["approval.decision"]


def test_the_store_is_best_effort(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("not a directory")
    t = tracing.Tracer(str(blocker / "traces.sqlite"))
    with t.trace("run") as tid:
        with t.span("plan"):
            pass
    assert t.get_trace(tid) is None and t.list_traces() == [] and t.count() == 0


def test_pruning_keeps_the_newest_runs(tmp_path):
    t = tracing.Tracer(str(tmp_path / "traces.sqlite"), max_runs=3)
    ids = []
    for i in range(5):
        with t.trace("run", i=i) as tid:
            t.event("answer")
        ids.append(tid)
    assert t.prune() == 2
    assert t.count() == 3
    kept = {x["trace_id"] for x in t.list_traces()}
    assert kept == set(ids[2:])
    con = sqlite3.connect(t.db_path)
    orphan = con.execute("SELECT COUNT(*) FROM spans WHERE trace_id NOT IN"
                         " (SELECT trace_id FROM traces)").fetchone()[0]
    con.close()
    assert orphan == 0


def test_list_is_newest_first_with_span_counts(tracer):
    with tracer.trace("run", mode="single") as a:
        tracer.event("answer")
    with tracer.trace("run", mode="graph") as b:
        tracer.event("plan")
        tracer.event("answer")
    listed = tracer.list_traces(limit=10)
    assert [x["trace_id"] for x in listed] == [b, a]
    assert [x["span_count"] for x in listed] == [2, 1]
    assert tracer.get_trace("nope") is None
