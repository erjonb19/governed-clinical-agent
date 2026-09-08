"""
data_quality.py
===============
Build-time data quality gates for the medallion Gold tables.

WHY THIS EXISTS
The FHIR build checks its work -- dedup before joins, a fan-out gate. The
hospital build did not check anything at all. It printed three counts and
trusted them, which is not the same as verifying them, and the difference is
invisible until it matters.

It mattered. The shipped hospital Gold has `readmit_hwr` and `ed_volume` 100%
NULL -- two measures the schema advertises, the README describes, and the agent
will happily write SQL against, with no data behind them. Nothing caught it:

  - The build printed "with HWR readmission: 0" and carried on.
  - The eval suite scored 35/35, because its ground truth is `reference_sql` run
    against THE SAME database. Ask for the lowest HWR rate and the reference
    returns NULL; the agent returns NULL; the case passes. The eval measures
    whether the agent writes the right SQL, not whether the data is right --
    and it cannot tell you the column is empty, because it is comparing the
    empty column against itself.

That is the gap these checks fill. An eval suite validates the AGENT. Nothing
was validating the DATA.

DESIGN
Checks return results rather than raising, so one build reports everything wrong
at once instead of stopping at the first problem -- a build that fails three
times in a row, one issue per run, wastes an afternoon.

Two severities, on a deliberate line:

  ERROR  the data is definitely wrong, and shipping it corrupts answers.
         Duplicate business keys, join fan-out, an empty table, a column with
         no data in it at all.

  WARN   the data looks unusual and might be fine. Partial coverage moves
         month to month; a row count can legitimately jump when the state
         filter is edited. Loud, not fatal.

"Entirely NULL" is an ERROR rather than a WARN on purpose. A column you chose to
include and map is never legitimately empty -- it means the source file changed
shape or a measure was renamed upstream, and the mapping is now stale. Partial
coverage is normal variation; zero coverage is a broken pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

ERROR = "ERROR"
WARN = "WARN"


@dataclass
class Check:
    name: str
    severity: str
    passed: bool
    detail: str

    def __str__(self) -> str:
        mark = "ok  " if self.passed else f"{self.severity}"
        return f"    [{mark:5}] {self.name}: {self.detail}"


def _one(con, sql, params=None):
    return con.execute(sql, params or []).fetchone()[0]


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def not_empty(con, table: str, minimum: int = 1) -> Check:
    n = _one(con, f"SELECT count(*) FROM {table}")
    return Check("not_empty", ERROR, n >= minimum,
                 f"{table} has {n} rows (need >= {minimum})")


def unique_key(con, table: str, key: str) -> Check:
    """The business key must identify one row.

    Every join in the hospital build is `LEFT JOIN ... USING (facility_id)`. If
    the key repeats on either side the result multiplies, and every count,
    average and ranking downstream is quietly wrong -- the single most damaging
    thing that can happen to this warehouse, and the hardest to notice, because
    the numbers stay plausible.
    """
    dupes = _one(con, f"""
        SELECT count(*) FROM (
            SELECT {key} FROM {table} GROUP BY {key} HAVING count(*) > 1)
    """)
    return Check(f"unique_key({key})", ERROR, dupes == 0,
                 f"{dupes} duplicated {key} values in {table}")


def no_fanout(con, child: str, parent: str) -> Check:
    """A joined table must never have more rows than what it was built from."""
    c, p = _one(con, f"SELECT count(*) FROM {child}"), _one(con, f"SELECT count(*) FROM {parent}")
    return Check(f"no_fanout({child})", ERROR, c <= p,
                 f"{child}={c} vs {parent}={p}")


def column_has_data(con, table: str, column: str) -> Check:
    """A mapped column with nothing in it is a broken mapping, not sparse data.

    This is the check that the shipped Gold fails on readmit_hwr and ed_volume.
    """
    n = _one(con, f"SELECT count({column}) FROM {table}")
    return Check(f"column_has_data({column})", ERROR, n > 0,
                 f"{n} non-null values" if n else
                 f"{column} is ENTIRELY NULL -- the source column was probably "
                 f"renamed or dropped upstream, so the mapping is stale")


def column_coverage(con, table: str, column: str, floor: float = 0.25) -> Check:
    """Coverage well below normal suggests a partial source file.

    The floor is a smoke alarm, not a control limit: real coverage here runs
    59-77%, so 25% flags a file that arrived truncated without firing every time
    CMS suppresses a few more hospitals than last month.
    """
    total = _one(con, f"SELECT count(*) FROM {table}")
    if not total:
        return Check(f"column_coverage({column})", WARN, True, "table empty; skipped")
    n = _one(con, f"SELECT count({column}) FROM {table}")
    frac = n / total
    return Check(f"column_coverage({column})", WARN, frac >= floor,
                 f"{n}/{total} = {frac:.1%} (floor {floor:.0%})")


def row_count_drift(con, table: str, history_table: str,
                    tolerance: float = 0.20) -> Optional[Check]:
    """Compare this build's row count against the previous vintage.

    Only possible because the Type 2 history exists -- before it, there was
    nothing to compare against and a 40% collapse in row count was
    indistinguishable from a normal month.

    Returns None on the first build, when there is genuinely nothing to compare.
    """
    prior = con.execute(f"""
        SELECT count(*) FROM {history_table}
         WHERE valid_from = (SELECT max(valid_from) FROM {history_table}
                              WHERE valid_from < (SELECT max(valid_from) FROM {history_table}))
    """).fetchone()[0]
    if not prior:
        return None
    now = _one(con, f"SELECT count(*) FROM {table}")
    drift = abs(now - prior) / prior
    return Check("row_count_drift", WARN, drift <= tolerance,
                 f"{prior} -> {now} ({drift:+.1%}; tolerance {tolerance:.0%}). "
                 f"A large jump is expected if the state filter changed.")


def values_in_set(con, table: str, column: str, allowed: set) -> Optional[Check]:
    """Everything in `column` is one of `allowed` -- e.g. the state filter held."""
    if not allowed:
        return None
    inlist = ", ".join(f"'{v}'" for v in sorted(allowed))
    bad = con.execute(f"""
        SELECT DISTINCT {column} FROM {table}
         WHERE {column} IS NOT NULL AND {column} NOT IN ({inlist}) LIMIT 5
    """).fetchall()
    return Check(f"values_in_set({column})", ERROR, not bad,
                 "all values in range" if not bad
                 else f"unexpected: {', '.join(str(b[0]) for b in bad)}")


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def report(checks: list[Check], label: str = "data quality") -> tuple[int, int]:
    """Print every result and return (errors, warnings)."""
    checks = [c for c in checks if c is not None]
    errors = [c for c in checks if not c.passed and c.severity == ERROR]
    warns = [c for c in checks if not c.passed and c.severity == WARN]
    print(f"\n  {label}:")
    for c in checks:
        print(c)
    if errors:
        print(f"\n  {len(errors)} ERROR(s) -- this data should not ship.")
    elif warns:
        print(f"\n  {len(warns)} warning(s) -- worth a look, not fatal.")
    return len(errors), len(warns)
