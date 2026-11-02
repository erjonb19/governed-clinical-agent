"""The de-identified FHIR build (PLAN_v2 §0.6 PR A, owner's decisions of
2026-10-31): a random stand-in key, Safe Harbor dates / ZIPs / ages, nothing
else copied, and scans that refuse the build when something identifying gets
through. Runs on a small synthetic Gold (the real one is not in CI)."""

import datetime as dt
import os

import duckdb
import pytest

import build_fhir_deid as B

N = 16
THIS_YEAR = dt.date.today().year


def make_gold(path: str) -> str:
    """A Gold with the real column names. Patient i: age 40+i (so ages are
    unique and identify the patient across builds), except p13 aged 95, p14
    born 92 years ago but died at 60, p15 in a low-population ZIP area."""
    con = duckdb.connect(path)
    con.execute(f"""
        CREATE TABLE gold_patient AS
        SELECT 'pat-' || i AS patient_id, 'Given' || i || 'x' AS given_name,
               'Family' || i || 'y' AS family_name,
               CASE WHEN i % 2 = 0 THEN 'female' ELSE 'male' END AS gender,
               CASE WHEN i = 14 THEN '{THIS_YEAR - 92}-02-03'
                    ELSE CAST({THIS_YEAR} - (CASE WHEN i = 13 THEN 95 ELSE 40 + i END) AS VARCHAR)
                         || '-06-1' || (i % 9) END AS birth_date,
               i = 14 AS is_deceased, 'M' AS marital_status, 'Springfield' AS city, 'MA' AS state,
               CASE WHEN i = 15 THEN '03601' WHEN i = 12 THEN NULL ELSE '0213' || (i % 10) END
                   AS postal_code,
               CASE WHEN i = 13 THEN 95 WHEN i = 14 THEN 60 ELSE 40 + i END AS age_years
        FROM range({N}) t(i)""")
    # p0: inpatient 2020-01-01..01-05, inpatient again 2020-01-20 (readmitted),
    # then outpatient 2020-03-01; everyone else one outpatient visit
    con.execute("""
        CREATE TABLE gold_encounter AS
        SELECT * FROM (VALUES
          ('enc-a', 'pat-0', 'female', 'MA', 'IMP', 'Inpatient', '2020-01-01', '2020-01-05', 4, 40),
          ('enc-b', 'pat-0', 'female', 'MA', 'IMP', 'Inpatient', '2020-01-20', '2020-01-22', 2, 40),
          ('enc-c', 'pat-0', 'female', 'MA', 'AMB', 'Checkup',   '2020-03-01', '2020-03-01', 0, 40))
          t(encounter_id, patient_id, gender, state, class_code, encounter_type,
            start_date, end_date, length_of_stay_days, age_at_encounter)
        UNION ALL
        SELECT 'enc-' || i, 'pat-' || i, 'male', 'MA', 'AMB', 'Checkup', '2021-05-0' || (1 + i % 9),
               '2021-05-0' || (1 + i % 9), 0, CASE WHEN i = 13 THEN 94 ELSE 40 + i END
        FROM range(1, 16) r(i)""")
    con.execute("""
        CREATE TABLE gold_condition AS
        SELECT 'cond-' || i AS condition_id, 'pat-' || i AS patient_id, 'enc-' || i AS encounter_id,
               'SNOMED' AS code_system, '38341003' AS code, 'Hypertension' AS condition_name,
               'resolved' AS clinical_status, '2019-01-01' AS onset_date,
               '2019-01-31' AS abatement_date, false AS is_active, 'male' AS gender, 'MA' AS state,
               CASE WHEN i = 13 THEN 93 ELSE 30 + i END AS age_at_onset
        FROM range(16) t(i)""")
    con.execute("""
        CREATE TABLE gold_observation AS
        SELECT 'obs-' || i AS observation_id, 'pat-' || i AS patient_id, 'enc-' || i AS encounter_id,
               'vital-signs' AS category, 'LOINC' AS code_system, '39156-5' AS code,
               'Body mass index' AS measure_name, 20.0 + i AS value_num, 'kg/m2' AS value_unit,
               '2021-05-01T10:00:00Z' AS effective_date, 'male' AS gender, 'MA' AS state,
               40 + i AS age_at_observation
        FROM range(16) t(i)""")
    con.execute("""
        CREATE TABLE gold_procedure AS
        SELECT 'proc-' || i AS procedure_id, 'pat-' || i AS patient_id, 'enc-' || i AS encounter_id,
               'SNOMED' AS code_system, '73761001' AS code, 'Colonoscopy' AS procedure_name,
               'completed' AS status, '2021-05-01' AS performed_date, 'male' AS gender, 'MA' AS state
        FROM range(16) t(i)""")
    con.execute("""
        CREATE TABLE gold_medication AS
        SELECT 'med-' || i AS medication_request_id, 'pat-' || i AS patient_id,
               'enc-' || i AS encounter_id, 'RxNorm' AS code_system, '31738' AS code,
               'Lisinopril 10 MG' AS medication_name, 'active' AS status,
               '2021-05-01' AS authored_date, NULL::INTEGER AS dosage_text,
               'male' AS gender, 'MA' AS state
        FROM range(16) t(i)""")
    # the raw table that shares Gold's file today: must never be copied
    con.execute("CREATE TABLE bronze_patient AS SELECT patient_id, given_name FROM gold_patient")
    con.close()
    return path


@pytest.fixture
def gold(tmp_path):
    return make_gold(str(tmp_path / "fhir_gold.duckdb"))


def built(gold, tmp_path, name="fhir_deid.duckdb"):
    out = str(tmp_path / name)
    B.build(gold, out)
    return duckdb.connect(out, read_only=True)


def rows(con, sql):
    return con.execute(sql).fetchall()


# --- what the file holds ------------------------------------------------------

def test_the_file_holds_only_the_six_deid_tables(gold, tmp_path):
    con = built(gold, tmp_path)
    assert sorted(r[0] for r in rows(con, "SELECT table_name FROM information_schema.tables")) \
        == sorted(B.TABLES)


def test_no_original_id_name_place_or_full_date_survives(gold, tmp_path):
    con = built(gold, tmp_path)
    cols = rows(con, "SELECT table_name, column_name FROM information_schema.columns")
    names = {c for _, c in cols}
    for gone in ("patient_id", "encounter_id", "condition_id", "observation_id", "procedure_id",
                 "medication_request_id", "given_name", "family_name", "city", "postal_code",
                 "birth_date", "start_date", "end_date", "onset_date", "abatement_date",
                 "effective_date", "performed_date", "authored_date"):
        assert gone not in names, gone
    dump = repr([rows(con, f"SELECT * FROM {t}") for t in B.TABLES])
    for value in ("pat-", "enc-", "cond-", "Given", "Family", "Springfield", "02130", "03601"):
        assert value not in dump, value


# --- the stand-in key ---------------------------------------------------------

def test_pid_is_1_to_n_unique_and_on_every_row(gold, tmp_path):
    con = built(gold, tmp_path)
    assert sorted(r[0] for r in rows(con, "SELECT pid FROM deid_patient")) == list(range(1, N + 1))
    for t in B.TABLES:
        assert rows(con, f"SELECT count(*) FROM {t} WHERE pid IS NULL") == [(0,)], t


def test_pid_is_drawn_fresh_each_build_so_it_cannot_be_matched_across_builds(gold, tmp_path):
    # age identifies a fixture patient; the pid it gets must differ between builds
    a = dict(rows(built(gold, tmp_path, "a.duckdb"), "SELECT age_years, pid FROM deid_patient"))
    b = dict(rows(built(gold, tmp_path, "b.duckdb"), "SELECT age_years, pid FROM deid_patient"))
    assert a.keys() == b.keys() and a != b


def test_pid_does_not_follow_the_source_order(gold, tmp_path):
    # with the shuffle replaced by the identity, pids would follow patient_id
    # order; the real build must not (1 in 16! chance of a false failure)
    con = built(gold, tmp_path)
    ordered = [r[0] for r in rows(con, "SELECT pid FROM deid_patient ORDER BY age_years")]
    assert ordered != sorted(ordered)


def test_pid_is_joinable_across_tables(gold, tmp_path):
    con = built(gold, tmp_path)
    assert rows(con, "SELECT count(DISTINCT p.pid) FROM deid_patient p "
                     "JOIN deid_encounter e USING (pid)") == [(N,)]


# --- Safe Harbor --------------------------------------------------------------

def test_birth_dates_become_years_and_are_left_empty_for_anyone_90_or_over(gold, tmp_path):
    con = built(gold, tmp_path)
    by_age = dict(rows(con, "SELECT age_years, birth_year FROM deid_patient"))
    assert by_age[40] == THIS_YEAR - 40                  # a year, nothing finer
    assert by_age[90] is None                            # p13: 95, capped and no birth year
    assert by_age[60] is None                            # p14: died at 60, born 92 years ago


def test_zip_codes_are_cut_to_three_digits_and_000_in_low_population_areas(gold, tmp_path):
    con = built(gold, tmp_path)
    zips = {r[0] for r in rows(con, "SELECT zip3 FROM deid_patient")}
    assert zips == {"021", "000", None}                  # 0213x -> 021, 03601 -> 000, NULL kept


def test_ages_are_capped_at_90_everywhere(gold, tmp_path):
    con = built(gold, tmp_path)
    for t, c in (("deid_patient", "age_years"), ("deid_encounter", "age_at_encounter"),
                 ("deid_condition", "age_at_onset"), ("deid_observation", "age_at_observation")):
        assert rows(con, f"SELECT max({c}) FROM {t}")[0][0] <= 90, (t, c)
    assert rows(con, "SELECT count(*) FROM deid_patient WHERE age_years = 90") == [(1,)]


def test_event_dates_become_years(gold, tmp_path):
    con = built(gold, tmp_path)
    assert rows(con, "SELECT DISTINCT effective_year FROM deid_observation") == [(2021,)]
    assert rows(con, "SELECT DISTINCT onset_year, abatement_year FROM deid_condition") \
        == [(2019, 2019)]


def test_intervals_are_computed_before_the_dates_are_dropped(gold, tmp_path):
    con = built(gold, tmp_path)
    p0 = rows(con, "SELECT e.start_year, e.class_code, e.days_to_next_encounter, e.readmitted_30d "
                   "FROM deid_encounter e JOIN deid_patient p USING (pid) "
                   "WHERE p.age_years = 40 ORDER BY e.days_to_next_encounter NULLS LAST")
    # 01-05 -> 01-20: 15 days, readmitted; 01-22 -> 03-01: 39 days, no later
    # inpatient stay; the outpatient visit is not an admission
    assert p0 == [(2020, "IMP", 15, True), (2020, "IMP", 39, False), (2020, "AMB", None, None)]
    assert rows(con, "SELECT DISTINCT days_from_onset_to_abatement FROM deid_condition") == [(30,)]


# --- the scans refuse a bad build ---------------------------------------------

def _with(monkeypatch, table, extra):
    gold_t, select = B.SELECTS[table]
    monkeypatch.setitem(B.SELECTS, table, (gold_t, select + ", " + extra))


@pytest.mark.parametrize("table, extra, problem", [
    ("deid_patient", "src.city AS city", "place or a personal detail"),
    ("deid_patient", "src.patient_id AS source_id", "identifier"),
    ("deid_encounter", "src.start_date AS visit_date", "full date"),
    ("deid_patient", "src.family_name AS surname_hint", "place or a personal detail"),
    ("deid_patient", "src.given_name AS note", "equal a real name"),
    ("deid_patient", "src.birth_date AS note", "full-date-shaped"),
    ("deid_patient", "src.postal_code AS note", "5-digit-ZIP-shaped"),
    ("deid_condition", "src.encounter_id AS note", "equal a real name"),
])
def test_a_build_that_lets_something_identifying_through_is_refused(
        gold, tmp_path, monkeypatch, table, extra, problem):
    _with(monkeypatch, table, extra)
    out = tmp_path / "fhir_deid.duckdb"
    with pytest.raises(B.DeidBuildError) as e:
        B.build(gold, str(out))
    assert problem in str(e.value)
    assert not out.exists() and not os.path.exists(str(out) + ".building")


def test_a_low_population_zip_area_must_read_000(gold, tmp_path, monkeypatch):
    monkeypatch.setattr(B, "_zip3", lambda col: f"left(src.{col}, 3)")
    gold_t, _ = B.SELECTS["deid_patient"]
    monkeypatch.setitem(B.SELECTS, "deid_patient", (gold_t, "m.pid, left(src.postal_code, 3) AS zip3, "
                                                    "LEAST(src.age_years, 90) AS age_years, "
                                                    "NULL::BIGINT AS birth_year"))
    with pytest.raises(B.DeidBuildError, match="low-population"):
        B.build(gold, str(tmp_path / "x.duckdb"))


def test_a_birth_year_for_someone_over_89_is_refused(gold, tmp_path, monkeypatch):
    gold_t, _ = B.SELECTS["deid_patient"]
    monkeypatch.setitem(B.SELECTS, "deid_patient", (gold_t, "m.pid, NULL::VARCHAR AS zip3, "
                                                    "LEAST(src.age_years, 90) AS age_years, "
                                                    "year(CAST(src.birth_date AS DATE)) AS birth_year"))
    with pytest.raises(B.DeidBuildError, match="birth_year"):
        B.build(gold, str(tmp_path / "x.duckdb"))


def test_an_uncapped_age_is_refused(gold, tmp_path, monkeypatch):
    gold_t, select = B.SELECTS["deid_observation"]
    monkeypatch.setitem(B.SELECTS, "deid_observation",
                        (gold_t, select.replace("LEAST(src.age_at_observation, 90)",
                                                "src.age_at_observation + 60")))
    with pytest.raises(B.DeidBuildError, match="an age above 90"):
        B.build(gold, str(tmp_path / "x.duckdb"))


def test_a_failed_build_leaves_the_previous_file_untouched(gold, tmp_path, monkeypatch):
    out = tmp_path / "fhir_deid.duckdb"
    B.build(gold, str(out))
    before = out.stat().st_mtime_ns
    _with(monkeypatch, "deid_patient", "src.given_name AS note")
    with pytest.raises(B.DeidBuildError):
        B.build(gold, str(out))
    assert out.stat().st_mtime_ns == before
