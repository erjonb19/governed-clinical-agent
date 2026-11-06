"""Measure how long guard-ALLOWED self-joins on gold_observation take.

Adversarial case 6 (2026-11-03). The guard checks what a query may return, not
what it costs: a self-join or cross join of a patient table passes it, and
nothing stops the database from working on it for minutes. This script times
such queries, at sizes up to the real FHIR Gold's, so a per-statement timeout
can be set from numbers rather than a guess.

A one-off measurement, not a test. It builds a SYNTHETIC gold_observation (the
real Gold is not in the repo), runs each query through sql_guard.validate_query
(to show it is allowed and to get the exact SQL the tool would run), then
executes that SQL on its own DuckDB connection with a watchdog that interrupts
it at --cap seconds.

Default full size: 223,503 observations over 1,180 patients. Both numbers are
taken from the real Synthea build (1,180 bundles; the observation count is
quoted in PR #46's PLAN_v2 §0.6 text, 223,503 observation ages). Not
re-measured here.

  python scripts/measure_compute_bomb.py                 # 1%, 10%, 100% of that size
  python scripts/measure_compute_bomb.py --cap 30 --scales 1
"""
from __future__ import annotations

import argparse
import os
import platform
import sys
import tempfile
import threading
import time

import duckdb

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sql_guard import validate_query  # noqa: E402

REAL_ROWS = 223_503
REAL_PATIENTS = 1_180

QUERIES = {
    # the shapes a planner could write for a "compare each patient's readings" question
    "baseline: count of observations":
        "SELECT count(*) AS n FROM gold_observation",
    "self-join on the patient key":
        "SELECT count(*) AS n FROM gold_observation a JOIN gold_observation b "
        "ON a.patient_id = b.patient_id",
    "self-join on the patient key, averaged":
        "SELECT avg(a.value_num) AS v FROM gold_observation a JOIN gold_observation b "
        "ON a.patient_id = b.patient_id",
    "three-way self-join on the patient key":
        "SELECT count(*) AS n FROM gold_observation a JOIN gold_observation b "
        "ON a.patient_id = b.patient_id JOIN gold_observation c ON b.patient_id = c.patient_id",
    "cross join":
        "SELECT count(*) AS n FROM gold_observation a, gold_observation b",
    "self-join on a safe column":
        "SELECT avg(a.value_num) AS v FROM gold_observation a JOIN gold_observation b "
        "ON a.measure_name = b.measure_name",
}


def build(path: str, rows: int, patients: int) -> None:
    con = duckdb.connect(path)
    con.execute(f"""
        CREATE TABLE gold_observation AS
        SELECT 'o' || i AS observation_id, 'p' || (i % {patients}) AS patient_id,
               'e' || (i // 7) AS encounter_id, 'vital-signs' AS category,
               'LOINC' AS code_system, CAST(i % 40 AS VARCHAR) AS code,
               'Measure ' || (i % 40) AS measure_name, (i % 997) / 10.0 AS value_num,
               'unit' AS value_unit, '2020-01-01' AS effective_date,
               CASE WHEN i % 2 = 0 THEN 'female' ELSE 'male' END AS gender, 'MA' AS state,
               40 + i % 50 AS age_at_observation
        FROM range({rows}) t(i)""")
    con.close()


def timed(con, sql: str, cap: float) -> tuple[str, float]:
    timer = threading.Timer(cap, con.interrupt)
    t0 = time.perf_counter()
    timer.start()
    try:
        con.execute(sql).fetchall()
        outcome = "done"
    except duckdb.InterruptException:
        outcome = "INTERRUPTED"
    except Exception as e:                       # out of memory, etc.
        outcome = f"error: {type(e).__name__}"
    finally:
        timer.cancel()
    return outcome, time.perf_counter() - t0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--scales", type=float, nargs="+", default=[0.01, 0.1, 1.0])
    ap.add_argument("--cap", type=float, default=60.0, help="interrupt each query after this many s")
    ap.add_argument("--memory-limit", default="4GB")
    args = ap.parse_args()

    print(f"duckdb {duckdb.__version__}, python {platform.python_version()}, "
          f"{os.cpu_count()} CPUs, memory_limit={args.memory_limit}, cap={args.cap:.0f}s")
    for name, sql in QUERIES.items():
        d = validate_query(sql)
        print(f"  guard: {'ALLOWED' if d.allowed else 'denied '}  {name}"
              + ("" if d.allowed else f"  ({d.reason[:80]})"))
    for scale in args.scales:
        rows, patients = int(REAL_ROWS * scale), max(1, int(REAL_PATIENTS * scale))
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "obs.duckdb")
            build(path, rows, patients)
            con = duckdb.connect(path, read_only=True)
            con.execute(f"SET memory_limit = '{args.memory_limit}'")
            print(f"\n{rows:,} observations, {patients:,} patients "
                  f"(~{rows // patients} per patient):")
            for name, sql in QUERIES.items():
                d = validate_query(sql)
                if not d.allowed:
                    continue
                outcome, secs = timed(con, d.safe_sql, args.cap)
                print(f"  {secs:8.2f}s  {outcome:12} {name}")
            con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
