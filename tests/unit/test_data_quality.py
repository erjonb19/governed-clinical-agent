"""Build-time data quality gates.

WHY THIS FILE EXISTS
The FHIR build checked its work; the hospital build checked nothing. It printed
three counts and trusted them, and the difference between printing a number and
asserting it stayed invisible until it mattered.

It mattered twice, in the same shipped Gold:

  readmit_hwr   entirely NULL, because the committed database was stale
  ed_volume     entirely NULL, because CMS publishes a text bucket
                ("low", "high", ...) and the build cast it to DOUBLE

Both columns were in the schema the agent is given, described in the README, and
queried by eval cases -- which passed. The eval's ground truth is
`reference_sql` run against the SAME database, so "what is the lowest HWR rate?"
compared NULL to NULL and scored a point. An eval suite validates the AGENT.
Nothing was validating the DATA.

These tests pin the gates that catch it, and specifically that `column_has_data`
is an ERROR rather than a warning: a column you chose to map is never
legitimately empty.
"""

import os
import sys

import duckdb
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import data_quality as dq


@pytest.fixture
def con():
    c = duckdb.connect(":memory:")
    c.execute("""
        CREATE TABLE profile AS
        SELECT * FROM (VALUES
            ('330101', 'NY', 3.0,  'high'),
            ('330202', 'NY', 4.0,  'low'),
            ('360303', 'MA', NULL, 'medium'))
        AS t(facility_id, state, star_rating, ed_volume)
    """)
    yield c
    c.close()


# --------------------------------------------------------------------------
# The checks that catch the real defects
# --------------------------------------------------------------------------

def test_an_entirely_null_column_is_an_error(con):
    """The exact shape of the ed_volume and readmit_hwr defects."""
    con.execute("ALTER TABLE profile ADD COLUMN readmit_hwr DOUBLE")
    c = dq.column_has_data(con, "profile", "readmit_hwr")

    assert not c.passed
    assert c.severity == dq.ERROR, (
        "a mapped column with no data is broken, not merely sparse -- warning "
        "about it is how it shipped in the first place")
    assert "ENTIRELY NULL" in c.detail


def test_a_partly_null_column_passes(con):
    """Sparse is normal. CMS suppresses measures for small hospitals, and 71%
    coverage is a healthy column, not a defect."""
    c = dq.column_has_data(con, "profile", "star_rating")
    assert c.passed and "2 non-null" in c.detail


def test_coverage_below_the_floor_only_warns(con):
    """Coverage moves month to month; a hard failure here would make the
    monthly build flaky for a legitimate reason."""
    c = dq.column_coverage(con, "profile", "star_rating", floor=0.99)
    assert not c.passed
    assert c.severity == dq.WARN


def test_coverage_above_the_floor_passes(con):
    assert dq.column_coverage(con, "profile", "star_rating", floor=0.5).passed


# --------------------------------------------------------------------------
# Joins
# --------------------------------------------------------------------------

def test_a_duplicated_business_key_is_an_error(con):
    """Every join in the hospital build is USING (facility_id). A repeated key
    multiplies rows, and every count and average downstream is wrong while
    still looking entirely plausible."""
    con.execute("INSERT INTO profile VALUES ('330101', 'NY', 5.0, 'low')")
    c = dq.unique_key(con, "profile", "facility_id")
    assert not c.passed and c.severity == dq.ERROR


def test_a_unique_key_passes(con):
    assert dq.unique_key(con, "profile", "facility_id").passed


def test_fanout_is_caught(con):
    con.execute("CREATE TABLE parent AS SELECT * FROM profile LIMIT 2")
    assert not dq.no_fanout(con, "profile", "parent").passed
    assert dq.no_fanout(con, "parent", "profile").passed


def test_an_empty_table_is_an_error(con):
    con.execute("CREATE TABLE empty_t AS SELECT * FROM profile WHERE false")
    c = dq.not_empty(con, "empty_t")
    assert not c.passed and c.severity == dq.ERROR


# --------------------------------------------------------------------------
# Scope
# --------------------------------------------------------------------------

def test_a_value_outside_the_filter_is_caught(con):
    """If the state filter silently stopped applying, the Gold would quietly
    grow to the whole country and every 'in our region' answer would be wrong."""
    c = dq.values_in_set(con, "profile", "state", {"NY", "MA"})
    assert c.passed
    assert not dq.values_in_set(con, "profile", "state", {"NY"}).passed


def test_an_empty_allowed_set_skips_the_check(con):
    """STATES = [] means 'all states', not 'no states allowed'."""
    assert dq.values_in_set(con, "profile", "state", set()) is None


# --------------------------------------------------------------------------
# Drift, which needs the history table
# --------------------------------------------------------------------------

def _history(con, counts):
    """A history table holding one row per hospital per vintage."""
    con.execute("CREATE TABLE hist (facility_id VARCHAR, valid_from DATE)")
    for vintage, n in counts.items():
        for i in range(n):
            con.execute("INSERT INTO hist VALUES (?, ?)", [f"h{i}", vintage])


def test_drift_is_skipped_on_the_first_build(con):
    """One vintage means there is genuinely nothing to compare against."""
    _history(con, {"2026-09-01": 3})
    assert dq.row_count_drift(con, "profile", "hist") is None


def test_a_stable_row_count_passes(con):
    _history(con, {"2026-08-01": 3, "2026-09-01": 3})
    c = dq.row_count_drift(con, "profile", "hist")
    assert c.passed


def test_a_collapse_in_row_count_is_flagged(con):
    """The failure this exists for: a source file arrives truncated and the
    Gold silently halves. Before the history table there was nothing to notice
    it against."""
    _history(con, {"2026-08-01": 10, "2026-09-01": 10})
    c = dq.row_count_drift(con, "profile", "hist")   # profile has 3 rows
    assert not c.passed
    assert c.severity == dq.WARN, "editing the state filter is a legitimate jump"
    assert "state filter" in c.detail


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def test_report_counts_errors_and_warnings_separately(con, capsys):
    checks = [
        dq.Check("a", dq.ERROR, False, "broken"),
        dq.Check("b", dq.WARN, False, "odd"),
        dq.Check("c", dq.ERROR, True, "fine"),
        None,
    ]
    errors, warns = dq.report(checks)
    assert (errors, warns) == (1, 1)
    assert "should not ship" in capsys.readouterr().out


def test_report_survives_a_skipped_check(con):
    """Checks that do not apply return None, and reporting must not trip on it."""
    assert dq.report([None, None]) == (0, 0)
