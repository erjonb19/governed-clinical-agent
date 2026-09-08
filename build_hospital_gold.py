"""
build_hospital_gold.py
======================
Hospital-level medallion. Lands five CMS Provider Data files, pivots the two
long ones (Unplanned Visits, Timely & Effective Care) to the measures we chose,
joins everything on Facility ID, and writes a provider-profile Gold.

Run from the repo root (after fetching the five CSVs into data\\) with venv311:
    python build_hospital_gold.py
    python build_hospital_gold.py --vintage 2026-10-01   # date this snapshot is
    python build_hospital_gold.py --rebuild              # start over, LOSES history

Output:
    medallion\\hospital_gold.duckdb
        gold_hospital_profile   the current snapshot, fully rebuilt each run
        gold_hospital_history   Type 2 history, ACCUMULATED across runs

THE DATABASE IS NOT DELETED ON EACH RUN
It used to be, and that is what made the monthly refresh amnesiac: CMS
republishes these measures every month, the values move, and each rebuild threw
the previous reading away. The profile table is still a full rebuild -- it is
only ever "now" -- but the history table beside it keeps what the profile
forgets, so the warehouse can answer "how has this changed?" and not only "what
is it?". See gold_history.py for the merge and its invariants.

State filter: edit STATES below. Empty list = all states.
"""

from __future__ import annotations

import argparse
import os

import duckdb

import gold_history

DATA = "data"
OUT_DIR = "medallion"
GOLD_DB = os.path.join(OUT_DIR, "hospital_gold.duckdb")

# One-line geography switch. [] = all states. e.g. ["NY","NJ","CT","PA","MA"]
STATES: list[str] = ["NY", "NJ", "PA", "DE", "MD", "DC", "MA", "CT", "RI", "VT", "NH", "ME"]

# Measures to pull from the two long files, mapped to clean column names.
UNPLANNED_MEASURES = {
    "Hybrid Hospital-Wide All-Cause Readmission Measure (HWR)": "readmit_hwr",
    "Heart failure (HF) 30-Day Readmission Rate": "readmit_hf",
    "Pneumonia (PN) 30-Day Readmission Rate": "readmit_pn",
    "Acute Myocardial Infarction (AMI) 30-Day Readmission Rate": "readmit_ami",
    "Rate of readmission for chronic obstructive pulmonary disease (COPD) patients": "readmit_copd",
}
TIMELY_MEASURES = {
    "Average (median) time all patients spent in the emergency department before leaving from the visit, including psychiatric/mental health patients and patients who were transferred to another facility. A lower number of minutes is better": "ed_median_min",
    "Average (median) time psychiatric/mental health patients spent in the emergency department before leaving from the visit. A lower number of minutes is better": "ed_psych_median_min",
    "Left before being seen": "ed_left_before_seen_pct",
    "Emergency department volume": "ed_volume",
}

NUM = lambda c: (f"TRY_CAST(NULLIF(NULLIF(NULLIF(NULLIF(CAST({c} AS VARCHAR),"
                f"'Not Available'),'Not Applicable'),'N/A'),'') AS DOUBLE)")


def _state_clause(col: str = "state") -> str:
    if not STATES:
        return ""
    inlist = ", ".join(f"'{s}'" for s in STATES)
    return f"WHERE {col} IN ({inlist})"


def _pivot_select(measures: dict) -> str:
    # builds: max(CASE WHEN "Measure Name"=... THEN Score END) AS col, ...
    parts = []
    for mname, col in measures.items():
        safe = mname.replace("'", "''")
        parts.append(f"max(CASE WHEN \"Measure Name\" = '{safe}' THEN {NUM('Score')} END) AS {col}")
    return ",\n        ".join(parts)


def main(vintage: str | None = None, rebuild: bool = False):
    os.makedirs(OUT_DIR, exist_ok=True)
    # The database is NO LONGER deleted on every run. It used to be, which is
    # what made the monthly refresh amnesiac: gold_hospital_history lives in
    # this file, so removing it threw away every prior reading before the new
    # one was even built. The profile table is still fully rebuilt below --
    # CREATE OR REPLACE -- so the snapshot is as fresh as it ever was; only the
    # history survives now.
    if rebuild and os.path.exists(GOLD_DB):
        print(f"--rebuild: deleting {GOLD_DB}, INCLUDING its history")
        os.remove(GOLD_DB)
    con = duckdb.connect(GOLD_DB)
    con.execute("SET preserve_insertion_order=false")

    # --- spine: hospital general info ---
    con.execute(f"""
        CREATE OR REPLACE TABLE spine AS
        SELECT
            "Facility ID"   AS facility_id,
            "Facility Name" AS facility_name,
            "City/Town"     AS city,
            "State"         AS state,
            "ZIP Code"      AS zip,
            {NUM('"Hospital overall rating"')} AS star_rating
        FROM read_csv_auto('{DATA}/hospital_general.csv', all_varchar=true)
    """)

    # --- MSPB: one cost number per hospital ---
    con.execute(f"""
        CREATE OR REPLACE TABLE mspb AS
        SELECT "Facility ID" AS facility_id, {NUM('Score')} AS mspb_score
        FROM read_csv_auto('{DATA}/mspb.csv', all_varchar=true)
    """)

    # --- pivot the long files to chosen measures ---
    con.execute(f"""
        CREATE OR REPLACE TABLE readmissions AS
        SELECT "Facility ID" AS facility_id,
            {_pivot_select(UNPLANNED_MEASURES)}
        FROM read_csv_auto('{DATA}/unplanned_visits.csv', all_varchar=true)
        GROUP BY "Facility ID"
    """)
    con.execute(f"""
        CREATE OR REPLACE TABLE ed AS
        SELECT "Facility ID" AS facility_id,
            {_pivot_select(TIMELY_MEASURES)}
        FROM read_csv_auto('{DATA}/timely_effective_care.csv', all_varchar=true)
        GROUP BY "Facility ID"
    """)

    # --- join into the profile, filter to states ---
    con.execute(f"""
        CREATE OR REPLACE TABLE gold_hospital_profile AS
        SELECT s.*,
               m.mspb_score,
               r.readmit_hwr, r.readmit_hf, r.readmit_pn, r.readmit_ami, r.readmit_copd,
               e.ed_median_min, e.ed_psych_median_min, e.ed_left_before_seen_pct, e.ed_volume
        FROM spine s
        LEFT JOIN mspb m USING (facility_id)
        LEFT JOIN readmissions r USING (facility_id)
        LEFT JOIN ed e USING (facility_id)
        {_state_clause('s.state')}
    """)
    for t in ("spine", "mspb", "readmissions", "ed"):
        con.execute(f"DROP TABLE IF EXISTS {t}")

    n = con.execute("SELECT count(*) FROM gold_hospital_profile").fetchone()[0]
    rated = con.execute("SELECT count(*) FROM gold_hospital_profile WHERE star_rating IS NOT NULL").fetchone()[0]
    hwr = con.execute("SELECT count(*) FROM gold_hospital_profile WHERE readmit_hwr IS NOT NULL").fetchone()[0]
    scope = ", ".join(STATES) if STATES else "all states"
    print(f"gold_hospital_profile: {n} hospitals ({scope})")
    print(f"  with star rating: {rated}")
    print(f"  with HWR readmission: {hwr}")

    # Fold this snapshot into the Type 2 history BEFORE the connection closes.
    # The profile table above is a full rebuild -- it is only ever "now" -- so
    # without this step every monthly refresh discards the previous reading and
    # the warehouse can never answer a question about change. See gold_history.
    outcome = gold_history.historize(con, vintage=vintage)
    problems = gold_history.verify(con)
    if problems:
        # A drifted Type 2 table still answers queries, it just answers them
        # wrongly, and nothing about the result looks suspicious. Fail the build
        # rather than publish a history that quietly double-counts.
        con.close()
        raise SystemExit("history verification FAILED:\n  " + "\n  ".join(problems))

    if outcome["seeded"]:
        print(f"{gold_history.HISTORY_TABLE}: seeded {outcome['seeded']} rows "
              f"at vintage {outcome['vintage']}")
        print("  (history starts here -- trend questions need a second refresh)")
    else:
        print(f"{gold_history.HISTORY_TABLE}: vintage {outcome['vintage']} -- "
              f"{outcome['opened']} opened, {outcome['closed']} closed, "
              f"{outcome['unchanged']} unchanged")
    print(f"-> {GOLD_DB}")
    con.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[3])
    ap.add_argument("--vintage", default=None,
                    help="date this snapshot represents (YYYY-MM-DD); "
                         "defaults to today UTC")
    ap.add_argument("--rebuild", action="store_true",
                    help="delete the database first, DISCARDING all history")
    a = ap.parse_args()
    main(vintage=a.vintage, rebuild=a.rebuild)
