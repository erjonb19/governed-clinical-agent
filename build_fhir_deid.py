"""Build the de-identified FHIR layer (PLAN_v2 §0.6, PR A).

Reads the FHIR Gold (medallion/fhir_gold.duckdb, which also holds the raw
bronze_* tables) and writes a SEPARATE file, medallion/fhir_deid.duckdb,
holding only six deid_* tables. Nothing else is copied: no bronze_*, no
gold_*. The agent does not read this file yet (that is PR B); this PR builds
it, proves what is in it, and fails the build if anything identifying is.

HIPAA Safe Harbor, as the owner decided (2026-10-31):
  * patients: a random stand-in key `pid`, drawn fresh on every build from the
    OS's secure random source and NOT derived from any patient field. The
    mapping from patient_id lives only in memory for the length of the build
    and is never written anywhere, so a pid cannot be traced to a person or
    matched between two builds.
  * removed: names, city, every original ID (patient, encounter, condition,
    observation, procedure, medication request).
  * dates: reduced to the year. Birth year is left empty for anyone aged 90
    or over, and for anyone born 90 or more years ago (a deceased patient's
    age stops at death, but a birth year that old still indicates an age
    over 89).
  * ZIP codes: the first 3 digits, and "000" where that 3-digit area holds
    20,000 people or fewer (RESTRICTED_ZIP3).
  * ages: capped at 90 (stored as 90, shown as "90+").
  * age at an event (age_at_encounter / _onset / _observation): EMPTY for
    those same 90+ patients at every event, because the event's year minus
    the age at it would give back the withheld birth year (owner's decision
    2026-11-02). Their events and event years are kept; they drop out only of
    breakdowns by age at an event (e.g. "average age at onset" covers the
    other patients). Not set to 90, which would be a false age.
  * intervals the analyses need are computed HERE from the full dates, which
    are then dropped: days_to_next_encounter, readmitted_30d,
    days_from_onset_to_abatement (length_of_stay_days already exists).

Two scans then fail the build (exit 1, the output file removed):
  * schema: a column named like an identifier, a full date or a place finer
    than the state.
  * values: a string equal to a real name, full postal code or source ID; a
    full-date or 5-digit-ZIP-shaped string; a zip3 in a restricted area that
    is not "000"; a birth year for anyone 90+; an event carrying both a year
    and an age at it for anyone whose birth year is withheld.

  python build_fhir_deid.py [--source medallion/fhir_gold.duckdb]
                            [--out medallion/fhir_deid.duckdb]
"""
from __future__ import annotations

import argparse
import os
import re
import secrets
import sys

import duckdb

SOURCE = os.path.join("medallion", "fhir_gold.duckdb")
OUT = os.path.join("medallion", "fhir_deid.duckdb")
AGE_CAP = 90

# 3-digit ZIP areas of 20,000 people or fewer, which Safe Harbor requires be
# reported as "000". These are the 17 prefixes HHS lists in its de-
# identification guidance (2000 Census). TODO (PLAN_v2 0.6): recheck against
# current Census ZCTA population figures before real data is loaded.
RESTRICTED_ZIP3 = frozenset({
    "036", "059", "063", "102", "203", "556", "692", "790", "821", "823",
    "830", "831", "878", "879", "884", "890", "893",
})

# Columns that hold clinical vocabulary codes (SNOMED, LOINC, RxNorm): a
# 5-digit RxNorm code is ZIP-shaped, so these skip the ZIP-SHAPE rule only.
# They are still checked against every real name, postal code and source ID.
CODE_COLUMNS = frozenset({"code"})

# Clinical display names are allowed to end in _name; no other column is.
CLINICAL_NAMES = frozenset({"condition_name", "procedure_name", "medication_name",
                            "measure_name"})

TABLES = ("deid_patient", "deid_encounter", "deid_condition", "deid_observation",
          "deid_procedure", "deid_medication")


class DeidBuildError(Exception):
    """The built file holds something it must not; the build is refused."""


def _year(col: str) -> str:
    return f"year(TRY_CAST(src.{col} AS DATE))"


def _age(col: str) -> str:
    """The patient's own age: capped at 90 (stored 90, shown "90+")."""
    return f"LEAST(src.{col}, {AGE_CAP})"


def _event_age(col: str) -> str:
    """An age at an event. Empty for the 90+ group (m.over89): their event
    year minus their age at that event would give back the birth year that
    is withheld (owner's decision 2026-11-02 -- empty, not set to 90, which
    would be a false age; the event and its year are kept)."""
    return f"CASE WHEN m.over89 THEN NULL ELSE LEAST(src.{col}, {AGE_CAP}) END"


# Who counts as 90+: aged 90 or over, or born 90 or more years ago (a
# deceased patient's age stops at death, but such a birth year still
# indicates an age over 89). Their birth year is withheld, and so is their age
# at every event.
_OVER89 = (f"(p.age_years >= {AGE_CAP} OR "
           f"year(current_date) - year(TRY_CAST(p.birth_date AS DATE)) >= {AGE_CAP})")


def _zip3(col: str) -> str:
    restricted = ", ".join(f"'{z}'" for z in sorted(RESTRICTED_ZIP3))
    return (f"CASE WHEN src.{col} IS NULL OR length(trim(src.{col})) < 3 THEN NULL "
            f"WHEN left(trim(src.{col}), 3) IN ({restricted}) THEN '000' "
            f"ELSE left(trim(src.{col}), 3) END")


# deid table -> (gold table, SELECT list over `src` (the gold row) and `m`
# (the in-memory pid map)). Every column is listed: nothing passes by *.
SELECTS = {
    "deid_patient": ("gold_patient", f"""
        m.pid, src.gender,
        CASE WHEN m.over89 THEN NULL ELSE {_year('birth_date')} END AS birth_year,
        src.is_deceased, src.marital_status, src.state,
        {_zip3('postal_code')} AS zip3,
        {_age('age_years')} AS age_years"""),
    "deid_encounter": ("gold_encounter", f"""
        m.pid, src.gender, src.state, src.class_code, src.encounter_type,
        {_year('start_date')} AS start_year, {_year('end_date')} AS end_year,
        src.length_of_stay_days, {_event_age('age_at_encounter')} AS age_at_encounter,
        date_diff('day', TRY_CAST(src.end_date AS DATE),
                  lead(TRY_CAST(src.start_date AS DATE)) OVER w) AS days_to_next_encounter,
        CASE WHEN src.class_code = 'IMP' THEN
             coalesce(date_diff('day', TRY_CAST(src.end_date AS DATE),
                      lead(CASE WHEN src.class_code = 'IMP' THEN TRY_CAST(src.start_date AS DATE) END
                           IGNORE NULLS) OVER w) <= 30, false)
        END AS readmitted_30d"""),
    "deid_condition": ("gold_condition", f"""
        m.pid, src.code_system, src.code, src.condition_name, src.clinical_status,
        {_year('onset_date')} AS onset_year, {_year('abatement_date')} AS abatement_year,
        src.is_active, src.gender, src.state, {_event_age('age_at_onset')} AS age_at_onset,
        date_diff('day', TRY_CAST(src.onset_date AS DATE),
                  TRY_CAST(src.abatement_date AS DATE)) AS days_from_onset_to_abatement"""),
    "deid_observation": ("gold_observation", f"""
        m.pid, src.category, src.code_system, src.code, src.measure_name, src.value_num,
        src.value_unit, {_year('effective_date')} AS effective_year, src.gender, src.state,
        {_event_age('age_at_observation')} AS age_at_observation"""),
    "deid_procedure": ("gold_procedure", f"""
        m.pid, src.code_system, src.code, src.procedure_name, src.status,
        {_year('performed_date')} AS performed_year, src.gender, src.state"""),
    "deid_medication": ("gold_medication", f"""
        m.pid, src.code_system, src.code, src.medication_name, src.status,
        {_year('authored_date')} AS authored_year, src.dosage_text, src.gender, src.state"""),
}
# (table, age-at-event column, that event's year): the pairs that would give
# back a birth year
EVENT_AGE_YEAR = (("deid_encounter", "age_at_encounter", "start_year"),
                  ("deid_condition", "age_at_onset", "onset_year"),
                  ("deid_observation", "age_at_observation", "effective_year"))

# the patient's encounters in time order, for the interval columns
WINDOWS = {"deid_encounter": "WINDOW w AS (PARTITION BY src.patient_id "
                             "ORDER BY TRY_CAST(src.start_date AS DATE), src.encounter_id)"}


def _lit(path: str) -> str:
    """A path as a SQL string literal (ATTACH takes no parameters)."""
    return "'" + os.path.abspath(path).replace("'", "''") + "'"


def _random_pids(patient_ids: list) -> list[tuple]:
    """(patient_id, pid) pairs: 1..N in an order drawn from the OS's secure
    random source. Nothing about the patient goes into the number."""
    order = list(patient_ids)
    secrets.SystemRandom().shuffle(order)
    return [(p, i + 1) for i, p in enumerate(order)]


def build(source: str = SOURCE, out: str = OUT) -> dict:
    """Build `out` from `source`, scan it, and return {table: row count}.
    Raises DeidBuildError (and removes the half-built file) if a scan fails."""
    if not os.path.exists(source):
        raise FileNotFoundError(f"missing {source} -- build the FHIR Gold first")
    tmp = out + ".building"
    for p in (tmp, tmp + ".wal"):
        if os.path.exists(p):
            os.remove(p)
    con = duckdb.connect()                       # in memory: the pid map lives only here
    try:
        con.execute(f"ATTACH {_lit(source)} AS g (READ_ONLY)")
        con.execute(f"ATTACH {_lit(tmp)} AS d")
        ids = [r[0] for r in con.execute("SELECT patient_id FROM g.gold_patient").fetchall()]
        if len(set(ids)) != len(ids):
            raise DeidBuildError("gold_patient.patient_id is not unique")
        con.execute("CREATE TEMP TABLE pid_map (patient_id VARCHAR PRIMARY KEY, pid INTEGER, "
                    "over89 BOOLEAN)")
        con.executemany("INSERT INTO pid_map (patient_id, pid) VALUES (?, ?)", _random_pids(ids))
        con.execute(f"UPDATE pid_map SET over89 = coalesce((SELECT {_OVER89} FROM g.gold_patient p "
                    f"WHERE p.patient_id = pid_map.patient_id), false)")
        counts = {}
        for table, (gold, select) in SELECTS.items():
            con.execute(f"CREATE TABLE d.{table} AS SELECT {select} "
                        f"FROM g.{gold} src JOIN pid_map m ON m.patient_id = src.patient_id "
                        f"{WINDOWS.get(table, '')}")
            counts[table] = con.execute(f"SELECT count(*) FROM d.{table}").fetchone()[0]
            gold_n = con.execute(f"SELECT count(*) FROM g.{gold}").fetchone()[0]
            if counts[table] != gold_n:
                raise DeidBuildError(f"{table}: {counts[table]} rows from {gold_n} in {gold} "
                                     f"(a row without a known patient)")
        scan(con)
        con.execute("DETACH d")
    except Exception:
        con.close()
        for p in (tmp, tmp + ".wal"):
            if os.path.exists(p):
                os.remove(p)
        raise
    con.close()
    os.replace(tmp, out)
    return counts


# ---------------------------------------------------------------------------
# the scans
# ---------------------------------------------------------------------------

_ID_LIKE = re.compile(r"(^|_)(id|ids)$|_key$|uuid|ssn|mrn")
_PLACE_OR_PERSON = re.compile(r"given|family|first_name|last_name|surname|city|county|"
                              r"postal|^zip$|zip5|zipcode|address|street|phone|email|fax|"
                              r"url|ip_addr|license|account|device|serial|plate|photo")
FULL_DATE = r"^\d{4}-\d{2}-\d{2}"
ZIP5 = r"^\d{5}(-\d{4})?$"


def scan_schema(con) -> list[str]:
    """Column names the de-identified tables must not have. Returns problems."""
    problems = []
    rows = con.execute("SELECT table_name, column_name FROM information_schema.columns "
                       "WHERE table_catalog = 'd' ORDER BY 1, 2").fetchall()
    tables = {t for t, _ in rows}
    if tables != set(TABLES):
        problems.append(f"tables {sorted(tables)} are not exactly {sorted(TABLES)}")
    for t, c in rows:
        c = c.lower()
        if c == "pid":
            continue
        if _ID_LIKE.search(c):
            problems.append(f"{t}.{c} looks like an identifier")
        elif c.endswith("_date") or c in ("date", "datetime", "timestamp"):
            problems.append(f"{t}.{c} looks like a full date")
        elif c.endswith("_name") and c not in CLINICAL_NAMES:
            problems.append(f"{t}.{c} looks like a person's or place's name")
        elif _PLACE_OR_PERSON.search(c):
            problems.append(f"{t}.{c} looks like a place or a personal detail")
    return problems


def scan_values(con) -> list[str]:
    """Values the de-identified tables must not hold. Returns problems (column
    and rule, never the value)."""
    problems = []
    # every real name, full postal code and source ID, held in memory only
    id_cols = [(t, c) for t, c in con.execute(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_catalog = 'g' AND table_name LIKE 'gold_%' "
        "AND (column_name LIKE '%\\_id' ESCAPE '\\') ").fetchall()]
    parts = [f"SELECT {c} AS v FROM g.{t}" for t, c in id_cols]
    parts += ["SELECT given_name FROM g.gold_patient", "SELECT family_name FROM g.gold_patient",
              "SELECT trim(postal_code) FROM g.gold_patient", "SELECT city FROM g.gold_patient"]
    con.execute("CREATE OR REPLACE TEMP TABLE forbidden AS SELECT DISTINCT v FROM ("
                + " UNION ALL ".join(parts) + ") WHERE v IS NOT NULL AND length(v) > 0")
    cols = con.execute("SELECT table_name, column_name, data_type FROM information_schema.columns "
                       "WHERE table_catalog = 'd' ORDER BY 1, 2").fetchall()
    for t, c, typ in cols:
        if typ != "VARCHAR":
            continue
        hit = con.execute(f"SELECT count(*) FROM d.{t} WHERE {c} IN (SELECT v FROM forbidden)"
                          ).fetchone()[0]
        if hit:
            problems.append(f"{t}.{c}: {hit} value(s) equal a real name, place, postal code or ID")
        if con.execute(f"SELECT count(*) FROM d.{t} WHERE regexp_matches({c}, ?)",
                       [FULL_DATE]).fetchone()[0]:
            problems.append(f"{t}.{c}: holds full-date-shaped values")
        if c not in CODE_COLUMNS and con.execute(
                f"SELECT count(*) FROM d.{t} WHERE regexp_matches(trim({c}), ?)",
                [ZIP5]).fetchone()[0]:
            problems.append(f"{t}.{c}: holds 5-digit-ZIP-shaped values")
    restricted = ", ".join(f"'{z}'" for z in sorted(RESTRICTED_ZIP3))
    if con.execute(f"SELECT count(*) FROM d.deid_patient WHERE zip3 IN ({restricted})").fetchone()[0]:
        problems.append("deid_patient.zip3: a low-population 3-digit area is not reported as 000")
    if con.execute("SELECT count(*) FROM d.deid_patient WHERE zip3 IS NOT NULL "
                   "AND NOT regexp_matches(zip3, '^\\d{3}$')").fetchone()[0]:
        problems.append("deid_patient.zip3: holds values that are not 3 digits")
    if con.execute(f"SELECT count(*) FROM d.deid_patient WHERE birth_year IS NOT NULL AND "
                   f"(age_years >= {AGE_CAP} OR year(current_date) - birth_year >= {AGE_CAP})"
                   ).fetchone()[0]:
        problems.append("deid_patient.birth_year: given for someone aged 90 or over")
    # A withheld birth year must not be recoverable as an event's year minus
    # the age at that event: for anyone whose birth year is empty, no event
    # may carry both (owner's decision 2026-11-02).
    for t, age_col, year_col in EVENT_AGE_YEAR:
        n = con.execute(f"SELECT count(*) FROM d.{t} e JOIN d.deid_patient p USING (pid) "
                        f"WHERE p.birth_year IS NULL AND e.{age_col} IS NOT NULL "
                        f"AND e.{year_col} IS NOT NULL").fetchone()[0]
        if n:
            problems.append(f"{t}: birth year can be derived ({year_col} - {age_col}) for "
                            f"{n} event(s) of patients whose birth year is withheld")
    for t, c, typ in cols:
        if c.startswith("age") and typ in ("BIGINT", "INTEGER", "DOUBLE"):
            if con.execute(f"SELECT count(*) FROM d.{t} WHERE {c} > {AGE_CAP}").fetchone()[0]:
                problems.append(f"{t}.{c}: an age above {AGE_CAP}")
    pid_n, pid_d = con.execute("SELECT count(*), count(DISTINCT pid) FROM d.deid_patient").fetchone()
    if pid_n != pid_d:
        problems.append("deid_patient.pid is not unique")
    return problems


def scan(con) -> None:
    problems = scan_schema(con) + scan_values(con)
    if problems:
        raise DeidBuildError("de-identified build refused:\n  " + "\n  ".join(problems))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--source", default=SOURCE)
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args()
    try:
        counts = build(args.source, args.out)
    except DeidBuildError as e:
        print(e, file=sys.stderr)
        return 1
    for t, n in counts.items():
        print(f"{t:18} {n:>8} rows")
    print(f"-> {args.out} (scans passed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
