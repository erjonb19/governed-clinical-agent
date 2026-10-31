"""PHI rules in sql_guard (phi_guard.py): every escape route is closed.

The owner's requirements (2026-10-30), one test per escape route: identifiers
and quasi-identifiers may not be returned at row level -- not through a join,
a subquery, a CTE, a UNION, a window, an alias, a cast, a string function or
SELECT * -- may not be grouped by or wrapped in MIN/MAX/LIST/STRING_AGG and
kin; dates only to the year; groups under 11 people are suppressed; and a second
check on the output refuses PHI however it was renamed, plus month/day dates
and zip-shaped values. The legitimate shapes the eval banks use still pass, and
the public hospital tables are untouched.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import phi_guard
from analytics_query_tool import AnalyticsQueryTool
from sql_guard import Decision, validate_query
from tests.security.fhir_fixture import build

LEAK = "SELECT patient_id, birth_date, postal_code FROM gold_patient"

BLOCKED = {
    "the original leak": LEAK,
    "names": "SELECT given_name, family_name FROM gold_patient",
    "SELECT *": "SELECT * FROM gold_patient",
    "SELECT * via alias": "SELECT p.* FROM gold_patient p",
    "join": ("SELECT c.condition_name, p.postal_code FROM gold_condition c "
             "JOIN gold_patient p ON c.patient_id = p.patient_id"),
    "subquery": "SELECT x.n FROM (SELECT given_name AS n FROM gold_patient) x",
    "CTE": "WITH p AS (SELECT * FROM gold_patient) SELECT city FROM p",
    "UNION": "SELECT gender FROM gold_patient UNION ALL SELECT family_name FROM gold_patient",
    "window": "SELECT first_value(given_name) OVER (PARTITION BY gender) AS w FROM gold_patient",
    "alias": "SELECT p.given_name AS who FROM gold_patient p",
    "cast": "SELECT CAST(birth_date AS VARCHAR) AS d FROM gold_patient",
    "cast to date": "SELECT CAST(birth_date AS DATE) AS d FROM gold_patient",
    "string function": "SELECT upper(city) AS c FROM gold_patient",
    "substring of a zip": "SELECT substr(postal_code, 1, 3) AS z3 FROM gold_patient",
    "concat": "SELECT given_name || ' ' || family_name AS full_name FROM gold_patient",
    "MIN": "SELECT gender, min(patient_id) AS m FROM gold_patient GROUP BY gender",
    "MAX": "SELECT max(encounter_id) AS m FROM gold_encounter",
    "MAX of a date": "SELECT max(birth_date) AS m FROM gold_patient",
    "ANY_VALUE": "SELECT gender, any_value(given_name) AS a FROM gold_patient GROUP BY gender",
    "LIST": "SELECT gender, list(given_name) AS l FROM gold_patient GROUP BY gender",
    "ARRAY_AGG": "SELECT gender, array_agg(patient_id) AS l FROM gold_patient GROUP BY gender",
    "STRING_AGG": "SELECT gender, string_agg(city, ',') AS s FROM gold_patient GROUP BY gender",
    "FIRST/LAST": "SELECT first(given_name) AS f, last(family_name) AS l FROM gold_patient",
    "full date": "SELECT onset_date FROM gold_condition",
    "date to the month": "SELECT strftime(CAST(birth_date AS DATE), '%Y-%m') AS ym FROM gold_patient",
    "city": "SELECT DISTINCT city FROM gold_patient",
    "identifier as GROUP BY key": "SELECT patient_id, count(*) AS n FROM gold_encounter GROUP BY patient_id",
    "identifier GROUP BY hidden inside": ("SELECT count(*) AS n FROM (SELECT patient_id FROM "
                                          "gold_encounter GROUP BY patient_id) t"),
    "GROUP BY ALL on an identifier": "SELECT patient_id, count(*) AS n FROM gold_encounter GROUP BY ALL",
    "record id": "SELECT observation_id, value_num FROM gold_observation",
    "comparison leaks a bit per row": "SELECT given_name = 'Ann' AS is_ann FROM gold_patient",
    "unlabelled column is an identifier": "SELECT some_new_column FROM gold_patient",
    "aggregate without a patient key": ("WITH t AS (SELECT gender FROM gold_patient) "
                                        "SELECT gender, count(*) AS n FROM t GROUP BY gender"),
    # --- bypasses found by adversarial review, each confirmed on the real data ---
    "correlated scalar subquery: id": (
        "SELECT (SELECT p2.patient_id FROM gold_patient p2 WHERE p2.patient_id = c.patient_id) AS pid, "
        "condition_name FROM gold_condition c"),
    "correlated scalar subquery: name": (
        "SELECT (SELECT given_name || family_name FROM gold_patient p2 WHERE p2.patient_id = c.patient_id) "
        "AS who, c.condition_name FROM gold_condition c"),
    "scalar subquery inside CASE": (
        "SELECT CASE WHEN true THEN (SELECT given_name FROM gold_patient p2 WHERE "
        "p2.patient_id = p.patient_id) END AS n FROM gold_patient p"),
    "scalar subquery: reshaped date": (
        "SELECT (SELECT replace(birth_date, '-', '') FROM gold_patient p2 WHERE "
        "p2.patient_id = p.patient_id) AS d, gender FROM gold_patient p"),
    "scalar subquery: reshaped zip": (
        "SELECT (SELECT postal_code || '.' FROM gold_patient p2 WHERE p2.patient_id = p.patient_id) "
        "AS z FROM gold_patient p"),
    "YEAR around date arithmetic": (
        "SELECT year(CAST(birth_date AS DATE) + to_years(month(CAST(birth_date AS DATE)) * 100 + "
        "day(CAST(birth_date AS DATE)))) - year(CAST(birth_date AS DATE)) AS mmdd, gender FROM gold_patient"),
    "date_diff year around day of year": (
        "SELECT date_diff('year', DATE '2000-01-01', DATE '2000-01-01' + "
        "to_years(dayofyear(CAST(birth_date AS DATE)))) AS doy FROM gold_patient"),
    "EXTRACT year around day": (
        "SELECT extract(year FROM CAST(birth_date AS DATE) + to_years(day(CAST(birth_date AS DATE)))) "
        "AS y FROM gold_patient"),
    "COUNT of a CASE on identifiers": (
        "SELECT count(CASE WHEN postal_code LIKE '021%' AND family_name LIKE 'A%' THEN 1 END) AS n "
        "FROM gold_patient"),
    "FILTER on a city": ("SELECT gender, count(*) FILTER (WHERE city = 'Boston' AND age_years > 90) AS n "
                         "FROM gold_patient GROUP BY gender"),
    "FILTER on a name": "SELECT count(*) FILTER (WHERE given_name = 'Bo157') AS n FROM gold_patient",
    "window count by city": ("SELECT DISTINCT n FROM (SELECT count(*) OVER (PARTITION BY city) AS n "
                             "FROM gold_patient) t WHERE n < 11"),
    "window count by zip": ("SELECT gender, age_years, count(*) OVER (PARTITION BY postal_code) AS n "
                            "FROM gold_patient"),
    "window aggregate over patients": ("SELECT gender, count(*) OVER (PARTITION BY gender) AS n "
                                       "FROM gold_patient"),
    "QUALIFY singling out by name": ("SELECT gender, age_years FROM gold_patient "
                                     "QUALIFY count(*) OVER (PARTITION BY given_name) = 1"),
    # --- second adversarial review ---
    "COLUMNS() by regex": ("SELECT COLUMNS('^patient_id$'), COLUMNS('^given_name$'), "
                           "replace(COLUMNS('^birth_date$'), '-', '/') FROM gold_patient"),
    "COLUMNS() by lambda": "SELECT COLUMNS(c -> c = 'family_name') FROM gold_patient",
    "a condition hidden in SUM": "SELECT SUM(CASE WHEN age_years > 105 THEN 1 ELSE 0 END) AS n FROM gold_patient",
    "a condition hidden in COUNT": "SELECT count(CASE WHEN age_years = 115 THEN 1 END) AS n FROM gold_patient",
    # --- third adversarial review ---
    "positional column": "SELECT #3 FROM gold_patient",
    "positional GROUP BY": "SELECT #3, count(*) FROM gold_patient GROUP BY #3",
    "query_table() hides the table": "SELECT * FROM query_table('gold_patient')",
    "query() runs a string the guard never parses": "SELECT * FROM query('SELECT given_name FROM gold_patient')",
    "query() lists the catalog": "SELECT table_name FROM query('SELECT table_name FROM duckdb_tables()')",
    "a spoofed person key": (
        "SELECT t.gender, t.measure_name, avg(t.value_num) AS v, count(*) AS n FROM "
        "(SELECT patient_id || observation_id AS patient_id, gender, measure_name, value_num "
        "FROM gold_observation) t GROUP BY 1, 2"),
    "a fan-out hiding one source's key": (
        "SELECT t.gender, t.measure_name, avg(t.value_num) AS v FROM (SELECT p.patient_id, o.gender, "
        "o.measure_name, o.value_num FROM gold_observation o JOIN gold_patient p ON o.gender = p.gender) t "
        "GROUP BY 1, 2"),
}

ALLOWED = {
    "count of people": "SELECT count(DISTINCT patient_id) AS n FROM gold_condition WHERE is_active",
    "count of records": "SELECT count(*) AS n FROM gold_encounter",
    "year of birth": "SELECT YEAR(CAST(birth_date AS DATE)) AS y, count(*) AS n FROM gold_patient GROUP BY 1",
    "EXTRACT year": "SELECT EXTRACT(YEAR FROM CAST(onset_date AS DATE)) AS y, count(*) AS n FROM gold_condition GROUP BY 1",
    "age in whole years": ("SELECT date_diff('year', CAST(birth_date AS DATE), DATE '2026-01-01') AS age, "
                           "count(*) AS n FROM gold_patient GROUP BY 1"),
    "count of distinct cities": "SELECT count(DISTINCT city) AS n FROM gold_patient",
    "grouped safe column": "SELECT gender, count(*) AS n FROM gold_patient GROUP BY gender",
    "row-level safe columns": "SELECT gender, age_years FROM gold_patient LIMIT 5",
    "filter on an identifier": ("SELECT round(avg(o.value_num), 2) AS v FROM gold_observation o "
                                "WHERE o.patient_id IN (SELECT patient_id FROM gold_condition "
                                "WHERE condition_name = 'Prediabetes')"),
    "join on an identifier": ("SELECT count(DISTINCT c.patient_id) AS n FROM gold_condition c "
                              "JOIN gold_patient p ON c.patient_id = p.patient_id WHERE p.age_years >= 65"),
    "top-N by count": ("SELECT condition_name FROM gold_condition GROUP BY condition_name "
                       "ORDER BY count(*) DESC, condition_name ASC LIMIT 5"),
    "FILTER clause": ("SELECT round(100.0 * count(*) FILTER (WHERE class_code = 'EMER') / count(*), 2) "
                      "AS v FROM gold_encounter"),
    "CTE keeping the patient key": ("WITH t AS (SELECT patient_id, gender FROM gold_patient) "
                                    "SELECT gender, count(*) AS n FROM t GROUP BY gender"),
    "public hospital data": "SELECT facility_name, zip, city FROM gold_hospital_profile LIMIT 3",
    "percentage from two scalar counts": (
        "SELECT 100.0 * (SELECT count(*) FROM gold_patient WHERE gender = 'female') / "
        "(SELECT count(*) FROM gold_patient) AS pct"),
    "share of total over groups": ("SELECT gender, count(*) * 1.0 / SUM(count(*)) OVER () AS share "
                                   "FROM gold_patient GROUP BY gender"),
    "share of total from a grouped CTE": (
        "WITH g AS (SELECT gender, count(*) AS n FROM gold_patient GROUP BY gender) "
        "SELECT gender, n * 1.0 / SUM(n) OVER () AS share FROM g"),
    "sum over a union of counts": ("SELECT SUM(n) AS total FROM (SELECT count(*) AS n FROM gold_patient "
                                   "UNION ALL SELECT count(*) FROM gold_condition) x"),
}


@pytest.mark.parametrize("name", sorted(BLOCKED))
def test_an_escape_route_is_blocked(name):
    d = validate_query(BLOCKED[name])
    assert not d.allowed, name
    assert "PHI" in d.reason


@pytest.mark.parametrize("name", sorted(ALLOWED))
def test_a_legitimate_shape_still_passes(name):
    d = validate_query(ALLOWED[name])
    assert d.allowed, (name, d.reason)


def test_every_aggregate_over_patients_gets_the_small_cell_rule():
    d = validate_query("SELECT gender, count(*) AS n FROM gold_patient GROUP BY gender")
    assert "HAVING COUNT(DISTINCT gold_patient.patient_id) >= 11" in d.safe_sql
    nested = validate_query("SELECT max(n) AS m FROM (SELECT condition_name, count(*) AS n FROM "
                            "gold_condition GROUP BY condition_name) x")
    assert "HAVING COUNT(DISTINCT gold_condition.patient_id) >= 11" in nested.safe_sql


def test_the_hospital_tables_are_untouched():
    q = "SELECT facility_id, zip FROM gold_hospital_profile ORDER BY facility_id LIMIT 5"
    d = validate_query(q)
    assert d.allowed and d.phi_sql is None and "HAVING" not in d.safe_sql


def test_every_fhir_column_is_labelled():
    import duckdb
    real = os.path.join(os.path.dirname(__file__), "..", "..", "medallion", "fhir_gold.duckdb")
    if not os.path.exists(real):
        pytest.skip("FHIR Gold not built")
    con = duckdb.connect(real, read_only=True)
    try:
        for t in phi_guard.PROTECTED:
            cols = [r[0] for r in con.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = ?", [t]).fetchall()]
            unlabelled = [c for c in cols if c.lower() not in phi_guard.TABLES[t]]
            assert not unlabelled, (t, unlabelled)   # would be treated as identifiers
    finally:
        con.close()


# --- against data: small cells and the output check ----------------------------

@pytest.fixture
def tool(tmp_path):
    return AnalyticsQueryTool(db_path=build(str(tmp_path / "fhir.duckdb")), seed_demo=False)


def test_a_group_of_fewer_than_11_people_is_not_returned(tool):
    r = tool.execute({"sql": "SELECT gender, count(*) AS n FROM gold_patient GROUP BY gender"})
    assert r.success and r.output["rows"] == [{"gender": "female", "n": 12}]   # 3 men: suppressed
    assert "11" in r.output["phi_note"]


def test_a_count_of_fewer_than_11_people_is_suppressed(tool):
    r = tool.execute({"sql": "SELECT count(DISTINCT patient_id) AS n FROM gold_condition "
                             "WHERE condition_name = 'Rare disease'"})
    assert r.success and r.output["rows"] == [] and "suppressed" in r.output["phi_note"]
    big = tool.execute({"sql": "SELECT count(DISTINCT patient_id) AS n FROM gold_condition "
                               "WHERE condition_name = 'Hypertension'"})
    assert big.output["rows"] == [{"n": 14}]


def test_records_of_few_people_are_suppressed_too(tool):
    # 4 people with many records each still are 4 people
    r = tool.execute({"sql": "SELECT condition_name, count(*) AS n FROM gold_condition "
                             "GROUP BY condition_name ORDER BY n DESC"})
    names = [row["condition_name"] for row in r.output["rows"]]
    assert "Rare disease" not in names and "Hypertension" in names


def test_the_output_check_refuses_date_shaped_values_in_an_allowed_column(tool):
    r = tool.execute({"sql": "SELECT marital_status, count(*) AS n FROM gold_patient GROUP BY 1"})
    assert not r.success and "dates with month or day precision" in r.error
    assert "2019-03-05" not in r.error                  # never the value


def test_the_output_check_refuses_zip_shaped_values(tool):
    r = tool.execute({"sql": "SELECT state, count(*) AS n FROM gold_patient GROUP BY 1"})
    assert not r.success and "zip code" in r.error and "02134" not in r.error


def test_the_output_check_traces_a_renamed_identifier_through_lineage():
    # as if the pre-check had missed it: the output check must still refuse
    analysed = phi_guard.qualify(phi_guard.sqlglot.parse_one(
        "WITH p AS (SELECT given_name AS gn FROM gold_patient) SELECT upper(gn) AS harmless FROM p",
        read="duckdb"), schema=phi_guard.SCHEMA, dialect="duckdb",
        validate_qualify_columns=False).sql(dialect="duckdb")
    d = Decision(True, "allowed", phi_sql=analysed, phi_names=["harmless"])
    with pytest.raises(phi_guard.OutputRefused, match="identifier gold_patient.given_name"):
        phi_guard.check_output(d, ["harmless"], [{"harmless": "X"}])
    year = Decision(True, "allowed", phi_names=["y"], phi_sql=phi_guard.qualify(
        phi_guard.sqlglot.parse_one("SELECT YEAR(CAST(birth_date AS DATE)) AS y FROM gold_patient",
                                    read="duckdb"), schema=phi_guard.SCHEMA, dialect="duckdb",
        validate_qualify_columns=False).sql(dialect="duckdb"))
    phi_guard.check_output(year, ["y"], [{"y": 1980}])   # reduced to the year: fine


def test_a_column_count_mismatch_fails_closed():
    d = Decision(True, "allowed", phi_sql="SELECT gender FROM gold_patient", phi_names=["gender"])
    with pytest.raises(phi_guard.OutputRefused, match="fail closed"):
        phi_guard.check_output(d, ["gender", "surprise"], [])


def test_a_refusal_reaches_the_audit_log_without_the_value(tmp_path):
    import src.tools.base as tool_base
    from src.runtime.agent_runtime import AgentRuntime
    from src.runtime.audit_logger import AuditLogger
    saved = dict(tool_base._TOOL_REGISTRY)
    tool_base._TOOL_REGISTRY.pop("analytics.query_aggregate", None)
    try:
        audit = AuditLogger(log_dir=tmp_path / "audit", agent_id="t", max_buffer_size=1,
                            enable_console=False)
        rt = AgentRuntime(audit_logger=audit)
        rt.load_policy(os.path.join(os.path.dirname(__file__), "..", "..", "medicare_policy.yaml"))
        rt.register_tool(AnalyticsQueryTool(db_path=build(str(tmp_path / "f.duckdb")), seed_demo=False))
        res = rt.execute_tool("analytics.query_aggregate",
                              {"sql": "SELECT marital_status, count(*) AS n FROM gold_patient GROUP BY 1"})
        assert not res.result.success
        text = "\n".join(f.read_text() for f in (tmp_path / "audit").glob("*.jsonl"))
        assert "month or day precision" in text and "2019-03-05" not in text
    finally:
        tool_base._TOOL_REGISTRY.clear()
        tool_base._TOOL_REGISTRY.update(saved)


def test_the_output_check_fails_closed_on_a_lineage_leaf_it_cannot_place():
    # a correlated subquery: lineage reports the leaf as a bare column name
    q = ("SELECT (SELECT given_name FROM gold_patient p2 WHERE p2.patient_id = c.patient_id) AS who "
         "FROM gold_condition c")
    analysed = phi_guard.qualify(phi_guard.sqlglot.parse_one(q, read="duckdb"), schema=phi_guard.SCHEMA,
                                 dialect="duckdb", validate_qualify_columns=False).sql(dialect="duckdb")
    d = Decision(True, "allowed", phi_sql=analysed, phi_names=["who"])
    with pytest.raises(phi_guard.OutputRefused):
        phi_guard.check_output(d, ["who"], [{"who": "x"}])



def test_an_aliased_safe_column_is_not_refused_by_the_output_check(tool):
    # lineage names the leaf by the alias; it must still be placed as safe
    r = tool.execute({"sql": "SELECT round(avg(c.age_at_onset), 1) AS v FROM gold_condition c "
                             "WHERE c.patient_id IN (SELECT patient_id FROM gold_patient WHERE gender = 'female')"})
    assert r.success, r.error


def test_a_cross_join_cannot_pad_a_group_past_11(tool):
    # the unrelated gold_patient source has 15 people; the group itself has 4
    r = tool.execute({"sql": "SELECT c.condition_name, count(DISTINCT c.patient_id) AS n "
                             "FROM gold_patient pad, gold_condition c "
                             "WHERE c.condition_name = 'Rare disease' GROUP BY 1"})
    assert r.success and r.output["rows"] == []


def test_a_filtered_count_of_a_small_subgroup_is_nulled(tool):
    r = tool.execute({"sql": "SELECT count(*) FILTER (WHERE gender = 'male') AS men, "
                             "count(*) FILTER (WHERE gender = 'female') AS women FROM gold_patient"})
    assert r.success and r.output["rows"] == [{"men": None, "women": 12}]


def test_an_aggregate_sqlglot_does_not_model_is_still_suppressed(tool):
    r = tool.execute({"sql": "SELECT fsum(1) AS n FROM gold_patient WHERE gender = 'male'"})
    assert r.success and r.output["rows"] == []


@pytest.mark.parametrize("name", ["percentage from two scalar counts", "share of total over groups",
                                  "share of total from a grouped CTE", "sum over a union of counts"])
def test_plausible_ratio_queries_run(tool, name):
    r = tool.execute({"sql": ALLOWED[name]})
    assert r.success, (name, r.error)


def test_an_unaliased_derived_table_still_runs(tool):
    r = tool.execute({"sql": "SELECT gender, count(*) AS n FROM (SELECT gender, patient_id "
                             "FROM gold_patient) GROUP BY gender"})
    assert r.success and r.output["rows"] == [{"gender": "female", "n": 12}]


def test_a_filtered_count_keeps_its_column_name(tool):
    r = tool.execute({"sql": "SELECT count(*) FILTER (WHERE gender = 'female') FROM gold_patient"})
    assert r.success and len(r.output["columns"]) == 1
    assert "CASE" not in r.output["columns"][0]


def test_generator_table_functions_still_run(tool):
    r = tool.execute({"sql": "SELECT count(*) AS n FROM range(3)"})
    assert r.success and r.output["rows"] == [{"n": 3}]

