"""A database this tool cannot open must not take the service down.

WHY THIS FILE EXISTS
`AnalyticsQueryTool.__init__` connected to every configured Gold with no
handling for a connection that fails. Existing on disk is not the same as being
openable: DuckDB refuses a connection to a file another process holds, so a
running eval sweep or a second server was enough to raise straight out of the
constructor -- before FastAPI finished booting. The whole service died at
startup over one busy dataset, while the others were perfectly serveable.

That is not hypothetical. It happened twice while working on this repo: once
taking down the API server, once breaking `import mcp_server` mid-test-run.

Degrading is the right behaviour, but only if it stays honest -- a locked
warehouse reported as an empty one is worse than a crash, because it answers
"0 hospitals" instead of refusing.
"""

import os
import sys

import duckdb
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from analytics_query_tool import AnalyticsQueryTool


@pytest.fixture
def gold(tmp_path):
    """A tiny two-dataset Gold: one to lock, one that must survive."""
    paths = {}
    for name, table, n in (("hospital", "gold_hospital_profile", 3),
                           ("fhir", "gold_patient", 5)):
        p = str(tmp_path / f"{name}.duckdb")
        con = duckdb.connect(p)
        con.execute(f"CREATE TABLE {table} AS "
                    f"SELECT i AS id, 'x' AS label FROM range({n}) t(i)")
        con.close()
        paths[name] = p
    return paths


def _count(tool, table, dataset):
    return tool.execute({"sql": f"SELECT count(*) AS n FROM {table}",
                         "dataset": dataset})


# --------------------------------------------------------------------------
# A secondary dataset
# --------------------------------------------------------------------------

def test_a_locked_secondary_dataset_does_not_stop_construction(gold):
    holder = duckdb.connect(gold["fhir"])          # hold it, as an eval does
    try:
        tool = AnalyticsQueryTool(db_path=gold["hospital"], seed_demo=False,
                                  db_paths=gold)
        assert tool.datasets() == ["hospital"]
    finally:
        holder.close()


def test_the_other_datasets_still_serve(gold):
    holder = duckdb.connect(gold["fhir"])
    try:
        tool = AnalyticsQueryTool(db_path=gold["hospital"], seed_demo=False,
                                  db_paths=gold)
        r = _count(tool, "gold_hospital_profile", "hospital")
        assert r.success and r.output["rows"] == [{"n": 3}]
    finally:
        holder.close()


def test_the_reason_is_kept_not_swallowed(gold):
    """A locked dataset and one that was never built are different problems
    with different fixes, and the visibility endpoints have to tell them apart."""
    holder = duckdb.connect(gold["fhir"])
    try:
        tool = AnalyticsQueryTool(db_path=gold["hospital"], seed_demo=False,
                                  db_paths=gold)
        errors = tool.dataset_errors()
        assert "fhir" in errors and errors["fhir"]
        assert "hospital" not in errors
    finally:
        holder.close()


def test_a_missing_dataset_reports_no_error(gold, tmp_path):
    """Never built is the normal case, not a failure -- it must not start
    showing up as one now that failures are recorded."""
    gold["ghost"] = str(tmp_path / "never_built.duckdb")
    tool = AnalyticsQueryTool(db_path=gold["hospital"], seed_demo=False,
                              db_paths=gold)
    assert "ghost" not in tool.datasets()
    assert "ghost" not in tool.dataset_errors()


# --------------------------------------------------------------------------
# The primary connection -- the one that actually killed the API server
# --------------------------------------------------------------------------

@pytest.fixture
def unopenable(tmp_path):
    """A file that exists and is not a database.

    DuckDB allows a second connection from the SAME process with the SAME
    configuration, so a lock held in-process does not reproduce the startup
    crash -- that one is cross-process, or read-only against read-write (which
    is what the secondary-dataset tests above exercise, and why they bite).

    Corruption reaches the identical code path deterministically and in one
    process: a file that is present but cannot be opened, which is exactly the
    condition the constructor has to survive.
    """
    p = tmp_path / "corrupt.duckdb"
    p.write_bytes(b"this is not a duckdb file, but it is definitely present " * 64)
    return str(p)


def test_an_unopenable_primary_database_still_constructs(unopenable):
    tool = AnalyticsQueryTool(db_path=unopenable, seed_demo=False, db_paths={})
    assert tool is not None


def test_an_unopenable_primary_is_reported_not_disguised(unopenable):
    """The failure mode to avoid: an empty in-memory fallback that looks like a
    warehouse with nothing in it. `backends()` has to say what happened, or the
    service answers "0 hospitals" to a question it should be refusing."""
    tool = AnalyticsQueryTool(db_path=unopenable, seed_demo=False, db_paths={})
    assert tool.backends()["local_db_error"]


def test_an_unopenable_primary_does_not_serve_stale_demo_rows(unopenable):
    """seed_demo must not fire on the fallback. Demo rows standing in for a real
    warehouse would answer a clinical question with invented numbers."""
    tool = AnalyticsQueryTool(db_path=unopenable, seed_demo=True, db_paths={})
    r = _count(tool, "gold_utilization", "hospital")
    assert not r.success, "the demo seed must not run on the fallback"


def test_other_datasets_survive_an_unopenable_primary(unopenable, gold):
    """Losing the default connection must not cost the named datasets too."""
    tool = AnalyticsQueryTool(db_path=unopenable, seed_demo=False, db_paths=gold)
    assert sorted(tool.datasets()) == ["fhir", "hospital"]
    r = _count(tool, "gold_patient", "fhir")
    assert r.success and r.output["rows"] == [{"n": 5}]


def test_a_healthy_primary_is_unaffected(gold):
    tool = AnalyticsQueryTool(db_path=gold["hospital"], seed_demo=False,
                              db_paths=gold)
    assert tool.backends()["local_db_error"] is None
    assert sorted(tool.datasets()) == ["fhir", "hospital"]
    r = _count(tool, "gold_patient", "fhir")
    assert r.success and r.output["rows"] == [{"n": 5}]
