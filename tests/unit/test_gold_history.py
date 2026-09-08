"""Type 2 history for the hospital Gold.

WHY THIS FILE EXISTS
`build_hospital_gold.py` rebuilds `gold_hospital_profile` from scratch, and
`data-refresh.yml` runs it monthly. Every refresh therefore discarded the
previous one: the warehouse could say what a hospital's readmission rate IS and
never what it WAS, despite having been fed the data to answer that.

These tests drive several simulated monthly refreshes -- values moving,
hospitals closing, hospitals opening, a hospital coming back -- because a Type 2
merge that is only ever run once is exactly the one that looks correct and is
not. Every interesting bug in this pattern needs at least two loads to appear.
"""

import os
import sys

import duckdb
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import gold_history as H


@pytest.fixture
def con():
    c = duckdb.connect(":memory:")
    yield c
    c.close()


def snapshot(con, rows):
    """Replace the source table, the way a monthly rebuild does."""
    con.execute(f"DROP TABLE IF EXISTS {H.SOURCE_TABLE}")
    con.execute(f"""
        CREATE TABLE {H.SOURCE_TABLE} (
            facility_id VARCHAR, facility_name VARCHAR, state VARCHAR,
            star_rating DOUBLE, readmit_hwr DOUBLE)
    """)
    con.executemany(
        f"INSERT INTO {H.SOURCE_TABLE} VALUES (?,?,?,?,?)", rows)


def current(con):
    return {r[0]: r[1] for r in con.execute(
        f"SELECT facility_id, star_rating FROM {H.HISTORY_TABLE} "
        f"WHERE is_current ORDER BY facility_id").fetchall()}


A = ("330101", "Mercy General", "NY", 3.0, 15.5)
B = ("330202", "St Anne", "NY", 4.0, 12.1)
C = ("360303", "Lakeside", "OH", 2.0, 19.9)


# --------------------------------------------------------------------------
# Seeding
# --------------------------------------------------------------------------

def test_first_load_seeds_every_row_as_current(con):
    snapshot(con, [A, B])
    out = H.historize(con, "2026-09-01")

    assert out["seeded"] == 2
    assert current(con) == {"330101": 3.0, "330202": 4.0}
    assert H.verify(con) == []


def test_first_load_opens_intervals_it_does_not_close(con):
    snapshot(con, [A])
    H.historize(con, "2026-09-01")
    row = con.execute(f"SELECT valid_from, valid_to, is_current "
                      f"FROM {H.HISTORY_TABLE}").fetchone()
    assert str(row[0]) == "2026-09-01"
    assert row[1] is None
    assert row[2] is True


# --------------------------------------------------------------------------
# The reason the table exists
# --------------------------------------------------------------------------

def test_a_changed_measure_opens_a_new_version(con):
    snapshot(con, [A, B])
    H.historize(con, "2026-09-01")

    improved = ("330101", "Mercy General", "NY", 4.0, 14.2)
    snapshot(con, [improved, B])
    out = H.historize(con, "2026-10-01")

    assert (out["closed"], out["opened"], out["unchanged"]) == (1, 1, 1)
    assert current(con)["330101"] == 4.0
    assert H.verify(con) == []


def test_the_old_value_is_still_there(con):
    """The entire point: the previous reading survives the refresh."""
    snapshot(con, [A])
    H.historize(con, "2026-09-01")
    snapshot(con, [("330101", "Mercy General", "NY", 4.0, 14.2)])
    H.historize(con, "2026-10-01")

    history = con.execute(
        f"SELECT star_rating, valid_from, valid_to FROM {H.HISTORY_TABLE} "
        f"ORDER BY valid_from").fetchall()
    assert [h[0] for h in history] == [3.0, 4.0]
    assert str(history[0][2]) == "2026-10-01", "the old version must be closed"
    assert history[1][2] is None


def test_as_of_answers_the_question_the_old_table_could_not(con):
    snapshot(con, [A])
    H.historize(con, "2026-09-01")
    snapshot(con, [("330101", "Mercy General", "NY", 4.0, 14.2)])
    H.historize(con, "2026-10-01")

    assert H.as_of(con, "2026-09-15", "star_rating") == [(3.0,)]
    assert H.as_of(con, "2026-10-15", "star_rating") == [(4.0,)]


def test_the_boundary_date_matches_exactly_one_version(con):
    """valid_to is EXCLUSIVE. If it were inclusive, a query on the changeover
    date would match both versions and quietly double-count."""
    snapshot(con, [A])
    H.historize(con, "2026-09-01")
    snapshot(con, [("330101", "Mercy General", "NY", 4.0, 14.2)])
    H.historize(con, "2026-10-01")

    assert H.as_of(con, "2026-10-01", "star_rating") == [(4.0,)]


# --------------------------------------------------------------------------
# Rows that do not change -- the majority, every month
# --------------------------------------------------------------------------

def test_an_unchanged_row_is_left_alone(con):
    snapshot(con, [A, B])
    H.historize(con, "2026-09-01")
    snapshot(con, [A, B])
    out = H.historize(con, "2026-10-01")

    assert (out["closed"], out["opened"], out["unchanged"]) == (0, 0, 2)
    assert con.execute(f"SELECT count(*) FROM {H.HISTORY_TABLE}").fetchone()[0] == 2


def test_nulls_do_not_count_as_a_change(con):
    """Still-missing must not read as changed, or every refresh would report a
    change for every hospital missing a measure -- many of them -- and bury the
    real changes in noise."""
    missing = ("330101", "Mercy General", "NY", None, None)
    snapshot(con, [missing])
    H.historize(con, "2026-09-01")
    snapshot(con, [missing])
    out = H.historize(con, "2026-10-01")

    assert out["closed"] == 0 and out["opened"] == 0
    assert con.execute(f"SELECT count(*) FROM {H.HISTORY_TABLE}").fetchone()[0] == 1


def test_a_value_moving_across_a_null_column_is_not_invisible(con):
    """The NULL trap that actually bites here.

    `concat_ws` SKIPS null arguments rather than emitting a separator for them,
    so ('x', NULL) and (NULL, 'x') both hash as 'x' -- two genuinely different
    rows with one hash. A measure moving between columns, or one appearing as
    another disappears, would be silently recorded as no change at all.

    The COALESCE sentinel is what prevents it: every column contributes a token,
    so position is preserved. Without it this test fails while the
    still-missing-is-unchanged case above keeps passing, which is precisely why
    that one is not sufficient on its own.
    """
    snapshot(con, [("330101", "Mercy General", "NY", 3.0, None)])
    H.historize(con, "2026-09-01")
    snapshot(con, [("330101", "Mercy General", "NY", None, 3.0)])
    out = H.historize(con, "2026-10-01")

    assert (out["closed"], out["opened"]) == (1, 1), (
        "a value moving across a null column must register as a change")


def test_null_becoming_a_value_is_a_change(con):
    """The other half of that: a measure first being reported is real news."""
    snapshot(con, [("330101", "Mercy General", "NY", None, None)])
    H.historize(con, "2026-09-01")
    snapshot(con, [("330101", "Mercy General", "NY", 3.0, 15.5)])
    out = H.historize(con, "2026-10-01")

    assert (out["closed"], out["opened"]) == (1, 1)
    assert current(con)["330101"] == 3.0


def test_hash_separator_prevents_a_field_boundary_collision(con):
    """Concatenating without a separator makes ('ab','c') and ('a','bc')
    identical, so a change that shifts text across a boundary is invisible."""
    snapshot(con, [("330101", "ab", "c", 1.0, 1.0)])
    H.historize(con, "2026-09-01")
    snapshot(con, [("330101", "a", "bc", 1.0, 1.0)])
    out = H.historize(con, "2026-10-01")

    assert out["opened"] == 1, "a shifted field boundary must register as a change"


# --------------------------------------------------------------------------
# Hospitals arriving and leaving
# --------------------------------------------------------------------------

def test_a_new_hospital_opens_a_version(con):
    snapshot(con, [A])
    H.historize(con, "2026-09-01")
    snapshot(con, [A, C])
    out = H.historize(con, "2026-10-01")

    assert (out["opened"], out["closed"]) == (1, 0)
    assert set(current(con)) == {"330101", "360303"}


def test_a_departed_hospital_is_closed_not_deleted(con):
    """Deleting it would erase the fact that it ever existed. Closing it records
    when it stopped being reported, which is itself the answer to a question."""
    snapshot(con, [A, C])
    H.historize(con, "2026-09-01")
    snapshot(con, [A])
    out = H.historize(con, "2026-10-01")

    assert out["closed"] == 1
    assert set(current(con)) == {"330101"}
    gone = con.execute(f"SELECT valid_to, is_current FROM {H.HISTORY_TABLE} "
                       f"WHERE facility_id = '360303'").fetchone()
    assert str(gone[0]) == "2026-10-01" and gone[1] is False
    # ...and it is still there when you ask about a date it was open.
    assert len(H.as_of(con, "2026-09-15")) == 2


def test_a_hospital_that_comes_back_gets_a_second_interval(con):
    """Three loads, and the gap has to survive: two separate intervals rather
    than one long one that wrongly claims it was reported all along."""
    snapshot(con, [A, C])
    H.historize(con, "2026-09-01")
    snapshot(con, [A])
    H.historize(con, "2026-10-01")
    snapshot(con, [A, C])
    H.historize(con, "2026-11-01")

    intervals = con.execute(
        f"SELECT valid_from, valid_to FROM {H.HISTORY_TABLE} "
        f"WHERE facility_id = '360303' ORDER BY valid_from").fetchall()
    assert len(intervals) == 2
    assert (str(intervals[0][0]), str(intervals[0][1])) == ("2026-09-01", "2026-10-01")
    assert (str(intervals[1][0]), intervals[1][1]) == ("2026-11-01", None)
    assert H.as_of(con, "2026-10-15", "facility_id") == [("330101",)]
    assert H.verify(con) == []


# --------------------------------------------------------------------------
# Operational safety
# --------------------------------------------------------------------------

def test_reloading_the_same_vintage_changes_nothing(con):
    """A monthly job that fails halfway and is retried must not manufacture a
    second version of every row."""
    snapshot(con, [A, B])
    H.historize(con, "2026-09-01")
    snapshot(con, [("330101", "Mercy General", "NY", 4.0, 14.2), B])
    H.historize(con, "2026-10-01")

    before = con.execute(f"SELECT count(*) FROM {H.HISTORY_TABLE}").fetchone()[0]
    out = H.historize(con, "2026-10-01")
    after = con.execute(f"SELECT count(*) FROM {H.HISTORY_TABLE}").fetchone()[0]

    assert before == after
    assert (out["opened"], out["closed"]) == (0, 0)
    assert H.verify(con) == []


def test_an_out_of_order_vintage_is_refused(con):
    """Loading an older snapshot after a newer one would open a version BEFORE
    the one it supersedes, and every 'as of' query would then match two rows.
    Refuse rather than silently corrupt the timeline."""
    snapshot(con, [A])
    H.historize(con, "2026-10-01")
    snapshot(con, [B])

    with pytest.raises(H.VintageError, match="older than"):
        H.historize(con, "2026-09-01")


def test_latest_vintage_reports_what_is_loaded(con):
    assert H.latest_vintage(con) is None
    snapshot(con, [A])
    H.historize(con, "2026-09-01")
    assert H.latest_vintage(con) == "2026-09-01"
    snapshot(con, [("330101", "Mercy General", "NY", 9.0, 1.0)])
    H.historize(con, "2026-10-01")
    assert H.latest_vintage(con) == "2026-10-01"


def test_verify_catches_a_corrupted_timeline(con):
    """verify() has to actually detect damage, or asserting on it every load is
    theatre. Two current versions of one key is the classic Type 2 failure."""
    snapshot(con, [A])
    H.historize(con, "2026-09-01")
    con.execute(f"""
        INSERT INTO {H.HISTORY_TABLE}
        SELECT facility_id, facility_name, state, 9.9, readmit_hwr,
               DATE '2026-10-01', NULL, TRUE, 'different-hash'
          FROM {H.HISTORY_TABLE}
    """)
    problems = H.verify(con)
    assert problems
    assert any("current version" in p for p in problems)


def test_verify_catches_backwards_intervals(con):
    snapshot(con, [A])
    H.historize(con, "2026-09-01")
    con.execute(f"UPDATE {H.HISTORY_TABLE} "
                f"SET valid_to = DATE '2026-08-01', is_current = FALSE")
    assert any("before they start" in p for p in H.verify(con))


# --------------------------------------------------------------------------
# Schema drift
# --------------------------------------------------------------------------

def test_a_new_measure_is_tracked_without_being_listed_anywhere(con):
    """Change detection reads the columns from the database. A hardcoded list is
    the kind of thing that silently stops tracking a column, and nobody notices
    until they ask why the history looks flat."""
    con.execute(f"CREATE TABLE {H.SOURCE_TABLE} AS "
                f"SELECT '330101' AS facility_id, 1.0 AS star_rating")
    H.historize(con, "2026-09-01")

    con.execute(f"DROP TABLE {H.SOURCE_TABLE}")
    con.execute(f"CREATE TABLE {H.SOURCE_TABLE} AS "
                f"SELECT '330101' AS facility_id, 1.0 AS star_rating")
    assert "sepsis_rate" not in H.tracked_columns(con)

    con.execute(f"ALTER TABLE {H.SOURCE_TABLE} ADD COLUMN sepsis_rate DOUBLE")
    assert "sepsis_rate" in H.tracked_columns(con)


# --------------------------------------------------------------------------
# The build must stay additive
# --------------------------------------------------------------------------

BUILD_SRC = os.path.join(os.path.dirname(__file__), "..", "..",
                         "build_hospital_gold.py")


@pytest.fixture(scope="module")
def build_source():
    with open(BUILD_SRC, encoding="utf-8") as fh:
        return fh.read()


def test_the_build_does_not_delete_the_database_unconditionally(build_source):
    """The original bug, in one line.

    `if os.path.exists(GOLD_DB): os.remove(GOLD_DB)` at the top of main() is
    what made the monthly refresh amnesiac -- the history table lives in that
    file, so deleting it threw away every prior reading before the new snapshot
    was even built. Deleting is still available, but only when asked for.
    """
    assert "os.remove(GOLD_DB)" in build_source, "the escape hatch should exist"
    assert "if rebuild and os.path.exists(GOLD_DB):" in build_source, (
        "the database must only be deleted when --rebuild is passed explicitly"
    )


def test_the_build_tables_are_re_runnable(build_source):
    """The file persists between runs now, so a plain CREATE TABLE would fail
    on the second refresh with 'table already exists'."""
    assert "\n        CREATE TABLE " not in build_source, (
        "use CREATE OR REPLACE TABLE -- the database is no longer deleted first"
    )
    assert build_source.count("CREATE OR REPLACE TABLE ") == 5


def test_the_build_historizes_and_verifies(build_source):
    """Merging without checking the invariants afterwards is how a Type 2 table
    drifts: it keeps answering, just wrongly."""
    assert "gold_history.historize(" in build_source
    assert "gold_history.verify(" in build_source
