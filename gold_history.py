"""
gold_history.py
===============
Slowly Changing Dimension (Type 2) history for the hospital Gold.

THE PROBLEM THIS SOLVES
`build_hospital_gold.py` ends with `CREATE TABLE gold_hospital_profile AS ...`
and `data-refresh.yml` runs it on the 1st of every month. So each refresh
OVERWRITES the last one. CMS republishes these measures monthly and the values
move -- star ratings change, readmission rates change, hospitals open and close
-- and every one of those changes was being thrown away the moment it arrived.

The warehouse could answer "what is Mercy General's readmission rate?" and could
never answer "how has it moved since March?", despite having been fed the data
to answer it. A dimension with no time in it cannot answer a question about
change, and change is most of what this data is for.

THE PATTERN
Type 2: don't update in place, close the old row and open a new one.

    facility_id  star_rating  valid_from   valid_to     is_current
    330101       3.0          2026-09-01   2026-11-01   false
    330101       4.0          2026-11-01   NULL         true

`valid_to` is EXCLUSIVE -- the row was true for [valid_from, valid_to). A
version's life is a half-open interval, so consecutive versions share a
boundary date without overlapping, and "as of date D" is one predicate with no
edge cases:

    WHERE valid_from <= D AND (valid_to IS NULL OR valid_to > D)

WHY NOT JUST APPEND EVERY SNAPSHOT
An append-only snapshot table (one full copy per month) is simpler and would
answer the same questions. Type 2 is used here because most rows do not change
between refreshes: appending would store ~748 rows a month to record perhaps a
few dozen actual changes, and -- more importantly -- it makes "when did this
change?" a query you have to derive by diffing adjacent months, rather than a
fact the table states directly.

CHANGE DETECTION
A row is "changed" when any tracked column differs. That comparison is done on a
hash of the whole row rather than column-by-column, so adding a measure to the
Gold does not mean remembering to add it to a comparison list -- the kind of
omission that silently stops tracking a column and is invisible until someone
asks why history looks flat.

NULLs are the subtle part: `NULL != NULL` in SQL, so a naive comparison reports
a change on every refresh for every hospital missing a measure -- which is many
of them. The hash normalises NULL to a sentinel so absent-and-still-absent
counts as unchanged, while absent-then-present is a real change.

WHAT THIS IS NOT
The measures here (readmission rates, star ratings) are periodic FACTS wearing a
dimension's clothes; a stricter model would split stable attributes (name, city,
state) into a Type 2 dimension and the measures into a monthly snapshot fact
keyed by vintage. That is the better warehouse, and a bigger change: it would
ripple into SCHEMA_DOC, the guard allowlist, and all 35 eval cases. This keeps
the existing wide profile shape and adds time to it.
"""

from __future__ import annotations

import datetime as _dt
from typing import Iterable, Optional

HISTORY_TABLE = "gold_hospital_history"
SOURCE_TABLE = "gold_hospital_profile"
BUSINESS_KEY = "facility_id"

# Columns the history adds on top of the source table's own.
SCD_COLUMNS = ("valid_from", "valid_to", "is_current", "row_hash")


class VintageError(ValueError):
    """Raised when a load would corrupt the timeline."""


def _today() -> str:
    return _dt.datetime.now(_dt.timezone.utc).date().isoformat()


def tracked_columns(con, table: str = SOURCE_TABLE) -> list[str]:
    """Every column of the source table, in order.

    Read from the database rather than hardcoded so a new measure is tracked the
    day it is added to the Gold, with no second place to remember to update.
    """
    rows = con.execute(f"PRAGMA table_info('{table}')").fetchall()
    if not rows:
        raise VintageError(f"{table} does not exist or has no columns")
    return [r[1] for r in rows]


def _hash_expr(columns: Iterable[str], prefix: str = "") -> str:
    """SQL expression hashing a whole row.

    Each value is cast to VARCHAR and NULL is replaced by a sentinel, because
    `NULL != NULL`: without this every hospital missing a measure would look
    changed on every single refresh. The separator is a control character that
    cannot occur in this data, so 'ab' + 'c' cannot collide with 'a' + 'bc'.
    """
    parts = [f"COALESCE(CAST({prefix}{c} AS VARCHAR), '\\x00NULL')" for c in columns]
    return f"md5(concat_ws('\\x01', {', '.join(parts)}))"


def history_exists(con) -> bool:
    return bool(con.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name = ?",
        [HISTORY_TABLE]).fetchone()[0])


def latest_vintage(con) -> Optional[str]:
    """The most recent vintage already loaded, or None if there is no history."""
    if not history_exists(con):
        return None
    row = con.execute(f"SELECT max(valid_from) FROM {HISTORY_TABLE}").fetchone()
    return row[0].isoformat() if row and row[0] else None


def historize(con, vintage: str | None = None, source: str = SOURCE_TABLE) -> dict:
    """Merge the current snapshot of `source` into the Type 2 history.

    Returns counts of what changed, which the caller prints and the refresh
    workflow can assert on.

    Idempotent by construction: re-running the same vintage compares the
    snapshot against itself, finds no differences, and writes nothing. That
    matters because a monthly job that fails halfway and is retried must not
    manufacture a second version of every row.
    """
    vintage = vintage or _today()
    cols = tracked_columns(con, source)
    if BUSINESS_KEY not in cols:
        raise VintageError(f"{source} has no {BUSINESS_KEY} to key history on")

    col_list = ", ".join(cols)
    src_hash = _hash_expr(cols, prefix="s.")

    previous = latest_vintage(con)
    if previous and vintage < previous:
        # Out-of-order loads silently corrupt a Type 2 table: the new row would
        # open before the row it supersedes, and every "as of" query would then
        # match two versions at once. Refuse rather than repair.
        raise VintageError(
            f"vintage {vintage} is older than the latest loaded vintage "
            f"{previous}; loading it would open a version before the one it "
            f"supersedes. Rebuild the history if you need to reload.")

    if not history_exists(con):
        con.execute(f"""
            CREATE TABLE {HISTORY_TABLE} AS
            SELECT {', '.join(f's.{c}' for c in cols)},
                   DATE '{vintage}' AS valid_from,
                   CAST(NULL AS DATE) AS valid_to,
                   TRUE              AS is_current,
                   {src_hash}        AS row_hash
              FROM {source} s
        """)
        n = con.execute(f"SELECT count(*) FROM {HISTORY_TABLE}").fetchone()[0]
        return {"vintage": vintage, "seeded": n, "opened": 0,
                "closed": 0, "unchanged": 0, "reopened": 0}

    # A staging table of the incoming snapshot with its hashes, so the three
    # comparisons below all read from the same computed values.
    con.execute("DROP TABLE IF EXISTS _incoming")
    con.execute(f"""
        CREATE TEMP TABLE _incoming AS
        SELECT {', '.join(f's.{c}' for c in cols)}, {src_hash} AS row_hash
          FROM {source} s
    """)

    # 1. CHANGED and GONE: close the current version.
    #    Changed  -> the key is still present but the row differs.
    #    Gone     -> the key is absent from this snapshot (hospital closed, or
    #                dropped out of the state filter). Closing it records WHEN
    #                it disappeared instead of deleting the fact that it existed.
    closed = con.execute(f"""
        UPDATE {HISTORY_TABLE} AS h
           SET valid_to = DATE '{vintage}', is_current = FALSE
         WHERE h.is_current
           AND NOT EXISTS (
                 SELECT 1 FROM _incoming i
                  WHERE i.{BUSINESS_KEY} = h.{BUSINESS_KEY}
                    AND i.row_hash = h.row_hash)
    """).fetchone()
    closed_n = closed[0] if closed else 0

    # 2. OPEN a version for everything in the snapshot without a current row.
    #    That covers changed rows (just closed above), brand-new hospitals, and
    #    hospitals that had disappeared and came back -- all the same operation.
    opened = con.execute(f"""
        INSERT INTO {HISTORY_TABLE} ({col_list}, valid_from, valid_to, is_current, row_hash)
        SELECT {col_list}, DATE '{vintage}', NULL, TRUE, row_hash
          FROM _incoming i
         WHERE NOT EXISTS (
                 SELECT 1 FROM {HISTORY_TABLE} h
                  WHERE h.{BUSINESS_KEY} = i.{BUSINESS_KEY} AND h.is_current)
    """).fetchone()
    opened_n = opened[0] if opened else 0

    incoming_n = con.execute("SELECT count(*) FROM _incoming").fetchone()[0]
    con.execute("DROP TABLE IF EXISTS _incoming")

    return {
        "vintage": vintage,
        "seeded": 0,
        "opened": opened_n,
        "closed": closed_n,
        # Rows that arrived and needed no work -- the majority, every month.
        "unchanged": incoming_n - opened_n,
        "reopened": 0,
    }


def verify(con) -> list[str]:
    """Invariants a Type 2 table must satisfy. Returns violations, empty if sound.

    Worth asserting on every load rather than trusting the merge: a Type 2 table
    that has drifted still answers queries, it just answers them wrongly, and
    nothing about the result looks suspicious.
    """
    problems = []

    dupes = con.execute(f"""
        SELECT {BUSINESS_KEY}, count(*) c FROM {HISTORY_TABLE}
         WHERE is_current GROUP BY 1 HAVING count(*) > 1
    """).fetchall()
    if dupes:
        problems.append(f"{len(dupes)} keys have more than one current version "
                        f"(e.g. {dupes[0][0]})")

    open_ended = con.execute(f"""
        SELECT count(*) FROM {HISTORY_TABLE}
         WHERE (is_current AND valid_to IS NOT NULL)
            OR (NOT is_current AND valid_to IS NULL)
    """).fetchone()[0]
    if open_ended:
        problems.append(f"{open_ended} rows disagree about is_current vs valid_to")

    backwards = con.execute(f"""
        SELECT count(*) FROM {HISTORY_TABLE}
         WHERE valid_to IS NOT NULL AND valid_to <= valid_from
    """).fetchone()[0]
    if backwards:
        problems.append(f"{backwards} rows end on or before they start")

    # Two versions of one key covering the same instant -- the failure an
    # out-of-order load produces, and the one that makes "as of" queries
    # silently return two rows where they must return one.
    overlaps = con.execute(f"""
        SELECT count(*) FROM {HISTORY_TABLE} a JOIN {HISTORY_TABLE} b
          ON a.{BUSINESS_KEY} = b.{BUSINESS_KEY} AND a.rowid <> b.rowid
         WHERE a.valid_from < COALESCE(b.valid_to, DATE '9999-12-31')
           AND b.valid_from < COALESCE(a.valid_to, DATE '9999-12-31')
    """).fetchone()[0]
    if overlaps:
        problems.append(f"{overlaps // 2} pairs of versions overlap in time")

    return problems


def as_of(con, when: str, columns: str = "*") -> list[tuple]:
    """The profile as it stood on `when`.

    The whole point of the table, in one query -- and the reason `valid_to` is
    exclusive: this predicate needs no special case for the boundary date.
    """
    return con.execute(f"""
        SELECT {columns} FROM {HISTORY_TABLE}
         WHERE valid_from <= DATE '{when}'
           AND (valid_to IS NULL OR valid_to > DATE '{when}')
    """).fetchall()
