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
    # grouping by patient inside is allowed only when the outer aggregates over
    # it (decision 4); these are the shapes where it does not
    "per-patient group passed straight through": (
        "WITH x AS (SELECT patient_id, count(*) AS c FROM gold_encounter GROUP BY patient_id) "
        "SELECT patient_id, c FROM x"),
    "per-patient group re-grouped by patient": (
        "WITH x AS (SELECT patient_id, count(*) AS c FROM gold_encounter GROUP BY patient_id) "
        "SELECT patient_id, max(c) AS m FROM x GROUP BY patient_id"),
    "per-patient rows without the key": (
        "WITH x AS (SELECT patient_id, count(*) AS c FROM gold_encounter GROUP BY patient_id) "
        "SELECT c FROM x"),
    "per-patient group as the result": ("SELECT count(*) AS n FROM gold_encounter "
                                        "GROUP BY patient_id"),
    "grouped by a name inside": ("SELECT count(*) AS n FROM (SELECT given_name FROM gold_patient "
                                 "GROUP BY given_name) t"),
    # filters on identifying columns (decision 3)
    "WHERE on a name": ("SELECT c.condition_name FROM gold_condition c JOIN gold_patient p "
                        "ON c.patient_id = p.patient_id WHERE p.given_name = 'X'"),
    "WHERE on a patient id": "SELECT count(*) AS n FROM gold_encounter WHERE patient_id = 'abc'",
    "WHERE on a zip prefix": "SELECT count(*) AS n FROM gold_patient WHERE postal_code LIKE '02%'",
    "WHERE on an exact date": "SELECT count(*) AS n FROM gold_patient WHERE birth_date = '1980-01-01'",
    "WHERE on a month": ("SELECT count(*) AS n FROM gold_patient "
                         "WHERE month(CAST(birth_date AS DATE)) = 3"),
    "WHERE on a city in a subquery": (
        "SELECT count(*) AS n FROM gold_condition WHERE patient_id IN "
        "(SELECT patient_id FROM gold_patient WHERE city = 'Boston')"),
    "HAVING on a name": ("SELECT gender, count(*) AS n FROM gold_patient GROUP BY gender "
                         "HAVING max(given_name) > 'M'"),
    "JOIN ON a city": "SELECT count(*) AS n FROM gold_patient a JOIN gold_patient b ON a.city = b.city",
    # --- fifth adversarial review (decisions 3 and 4) ---
    "IN a constant subquery on a name": ("SELECT gender, age_years FROM gold_patient "
                                         "WHERE given_name IN (SELECT 'X') AND family_name IN (SELECT 'Y')"),
    "IN a constant subquery on an id": ("SELECT condition_name FROM gold_condition "
                                        "WHERE patient_id IN (SELECT 'abc')"),
    "IN unnest of a list of ids": ("SELECT condition_name FROM gold_condition "
                                   "WHERE patient_id IN (SELECT unnest(['abc']))"),
    "IN a union smuggling a constant": (
        "SELECT condition_name FROM gold_condition WHERE patient_id IN "
        "(SELECT patient_id FROM gold_patient WHERE age_years > 200 UNION SELECT 'abc')"),
    "a name flag tested with IS TRUE": (
        "SELECT d.gender, d.age_years FROM (SELECT gender, age_years, given_name = 'X' AS hit "
        "FROM gold_patient) d WHERE d.hit IS TRUE"),
    "a NULLIF on an id tested with IS NULL": (
        "SELECT d.gender FROM (SELECT gender, NULLIF(patient_id, 'abc') AS n FROM gold_patient) d "
        "WHERE d.n IS NULL"),
    "a key join to a disguised constant": (
        "SELECT p.gender, p.age_years FROM gold_patient p JOIN (SELECT 'abc' || substr(patient_id, 1, 0) "
        "AS k FROM gold_patient LIMIT 1) c ON p.patient_id = c.k"),
    "a name joined to a name": (
        "SELECT p.gender FROM gold_patient p JOIN (SELECT 'X' || substr(given_name, 1, 0) AS k "
        "FROM gold_patient LIMIT 1) c ON p.given_name = c.k"),
    "a key join to VALUES": ("SELECT p.gender FROM gold_patient p JOIN (VALUES ('abc')) v(patient_id) "
                             "ON p.patient_id = v.patient_id"),
    "NATURAL JOIN on names": ("SELECT gender, age_years FROM gold_patient NATURAL JOIN "
                              "(SELECT 'X' AS given_name, 'Y' AS family_name) c"),
    "ORDER BY a name": "SELECT gender, age_years FROM gold_patient ORDER BY given_name LIMIT 1",
    "ORDER BY a patient id": ("SELECT condition_name, gender FROM gold_condition "
                              "ORDER BY patient_id LIMIT 20"),
    "a per-patient group joined on a safe column": (
        "SELECT avg(d.k) AS a FROM (SELECT g.patient_id, count(DISTINCT c.patient_id) FILTER "
        "(WHERE c.condition_name = 'Rare') AS k FROM gold_patient g JOIN gold_condition c "
        "ON c.condition_name = 'Rare' GROUP BY g.patient_id) d"),
    "a per-patient scalar subquery": (
        "SELECT count(*) AS total, (SELECT count(*) FROM gold_encounter e GROUP BY e.patient_id "
        "ORDER BY 1 DESC LIMIT 1) AS mx FROM gold_patient"),
    "LIST of per-patient values": (
        "SELECT list(d.conds) AS l FROM (SELECT patient_id, string_agg(condition_name, '|') AS conds "
        "FROM gold_condition GROUP BY patient_id) d"),
    "ARG_MAX of per-patient values": (
        "SELECT arg_max(d.conds, d.n) AS a FROM (SELECT patient_id, string_agg(condition_name, '|') "
        "AS conds, count(*) AS n FROM gold_condition GROUP BY patient_id) d"),
    "MAX of a per-patient count": ("SELECT max(d.n) AS m FROM (SELECT patient_id, count(*) AS n "
                                   "FROM gold_encounter GROUP BY patient_id) d"),
    "a per-patient CTE also read row by row": (
        "WITH d AS (SELECT patient_id, count(*) AS n FROM gold_encounter GROUP BY patient_id) "
        "SELECT (SELECT avg(n) FROM d) AS a, x.n FROM d x"),
    "GROUP BY patient and gender inside": (
        "SELECT avg(n) AS a FROM (SELECT patient_id, gender, count(*) AS n FROM gold_encounter "
        "GROUP BY patient_id, gender) t"),
    "a boolean cast inside SUM": ("SELECT sum(CAST(condition_name = 'Rare' AS INT)) AS n "
                                  "FROM gold_condition"),
    "count_if": "SELECT count_if(condition_name = 'Rare') AS n FROM gold_condition",
    # --- owner's decision 1 (2026-10-31): aggregate-only ---
    "row-level safe columns": "SELECT gender, age_years FROM gold_patient LIMIT 5",
    "row-level rows of a rare cohort": (
        "SELECT p.gender FROM gold_patient p WHERE p.patient_id IN "
        "(SELECT c.patient_id FROM gold_condition c WHERE c.condition_name = 'Rare')"),
    "DISTINCT safe columns": "SELECT DISTINCT gender, age_years FROM gold_patient",
    "row-level rows in a UNION branch": ("SELECT count(*) AS n FROM gold_patient UNION ALL "
                                         "SELECT age_years FROM gold_patient"),
    # --- owner's decision 2 (2026-10-31): dates ---
    "a filter on an exact date literal": ("SELECT count(*) AS n FROM gold_encounter "
                                          "WHERE CAST(start_date AS DATE) >= DATE '2020-01-01'"),
    "a date range by literals": ("SELECT count(*) AS n FROM gold_encounter WHERE start_date "
                                 "BETWEEN '2020-01-01' AND '2020-03-31'"),
    "a date compared to today": ("SELECT count(*) AS n FROM gold_encounter "
                                 "WHERE CAST(start_date AS DATE) > current_date - 30"),
    "a day offset from a literal date": (
        "SELECT avg(date_diff('day', DATE '1900-01-01', CAST(birth_date AS DATE))) AS d "
        "FROM gold_patient"),
    "a day offset from a constant in a CTE": (
        "WITH x AS (SELECT DATE '1900-01-01' AS d) SELECT avg(date_diff('day', x.d, "
        "CAST(p.birth_date AS DATE))) AS v FROM gold_patient p, x"),
    "a date shifted then output": ("SELECT max(CAST(start_date AS DATE) + INTERVAL 1 DAY) AS d "
                                   "FROM gold_encounter"),
    "the month of a date in a filter": ("SELECT count(*) AS n FROM gold_encounter "
                                        "WHERE month(CAST(start_date AS DATE)) = 3"),
    "a date column in the output": "SELECT max(start_date) AS d FROM gold_encounter",
    # --- sixth adversarial review ---
    "a zip read back through range()": (
        "SELECT count(*) AS n FROM range((SELECT CAST(q.postal_code AS INT) FROM gold_patient q "
        "ORDER BY q.age_years LIMIT 1)) r"),
    "a name compared in WHERE via a scalar subquery": (
        "SELECT count(*) AS n FROM range(100000) r WHERE r.range < "
        "(SELECT ascii(q.family_name) FROM gold_patient q ORDER BY q.age_years LIMIT 1)"),
    "a correlated name lookup in WHERE": (
        "SELECT p.gender, p.age_years FROM gold_patient p WHERE 'X' = "
        "(SELECT q.family_name FROM gold_patient q WHERE q.patient_id = p.patient_id)"),
    "a birth date in a HAVING subquery": (
        "SELECT gender, count(*) AS n FROM gold_patient GROUP BY gender HAVING count(*) > "
        "(SELECT date_diff('day', DATE '1900-01-01', CAST(q.birth_date AS DATE)) FROM gold_patient q LIMIT 1)"),
    "a zip in a JOIN ON subquery": (
        "SELECT count(*) AS n FROM range(100000) r JOIN gold_patient p ON r.range < "
        "(SELECT CAST(q.postal_code AS INT) FROM gold_patient q LIMIT 1)"),
    "a nested OFFSET by a subquery": (
        "SELECT count(*) AS n FROM (SELECT r.range FROM range(100000) r OFFSET "
        "(SELECT count(*) FROM gold_patient)) t"),
    "a nested LIMIT by a subquery": (
        "SELECT count(*) AS n FROM (SELECT r.range FROM range(100000) r LIMIT "
        "(SELECT count(*) FROM gold_patient)) t"),
    "a date through generate_series()": (
        "SELECT count(*) AS n FROM generate_series(1, (SELECT year(CAST(q.birth_date AS DATE)) * 1000 + "
        "month(CAST(q.birth_date AS DATE)) FROM gold_patient q LIMIT 1)) g"),
    "quantile_disc of per-patient values": (
        "SELECT quantile_disc(t.n, 1.0) AS q FROM (SELECT e.patient_id, count(*) AS n "
        "FROM gold_encounter e GROUP BY e.patient_id) t"),
    "median of per-patient values": (
        "SELECT median(t.n) AS q FROM (SELECT e.patient_id, count(*) AS n "
        "FROM gold_encounter e GROUP BY e.patient_id) t"),
    "bit_or of per-patient values": (
        "SELECT bit_or(t.n) AS q FROM (SELECT e.patient_id, count(*) AS n "
        "FROM gold_encounter e GROUP BY e.patient_id) t"),
    "JOIN ON an id compared to a value": (
        "SELECT count(*) AS n FROM gold_condition c JOIN gold_patient p "
        "ON c.patient_id = p.patient_id AND p.patient_id > 'm'"),
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
    # --- fourth adversarial review ---
    "ARRAY(subquery) through LATERAL": ("SELECT ARRAY(SELECT v FROM gold_patient p, "
                                        "LATERAL (SELECT p.given_name AS v) l) AS a"),
    "unnest of ARRAY through LATERAL": ("SELECT unnest(ARRAY(SELECT v FROM gold_patient p, "
                                        "LATERAL (SELECT p.given_name AS v) l)) AS a"),
    "a struct of identifiers in an ARRAY": (
        "SELECT ARRAY(SELECT v FROM gold_patient p, LATERAL (SELECT {'i': p.patient_id, "
        "'c': p.city} AS v) l) AS a"),
    "ARRAY over a LATERAL CTE": ("WITH c AS (SELECT v FROM gold_patient p, LATERAL (SELECT "
                                 "p.birth_date AS v) l) SELECT ARRAY(SELECT v FROM c) AS a"),
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
    # filters that decision 3 allows
    "WHERE on the year": "SELECT count(*) AS n FROM gold_patient WHERE YEAR(CAST(birth_date AS DATE)) < 1960",
    "WHERE on age in whole years": "SELECT count(*) AS n FROM gold_patient WHERE age_years >= 65",
    "WHERE on age from a date diff": (
        "SELECT count(*) AS n FROM gold_patient "
        "WHERE date_diff('year', CAST(birth_date AS DATE), DATE '2026-01-01') >= 65"),
    "WHERE a date IS NULL": "SELECT count(*) AS n FROM gold_condition WHERE abatement_date IS NULL",
    "a semi-join through a derived key": (
        "SELECT count(*) AS n FROM gold_condition c WHERE c.patient_id IN "
        "(SELECT t.patient_id FROM (SELECT patient_id FROM gold_encounter WHERE class_code = 'EMER') t)"),
    # owner's decision 2: intervals between two dates, aggregated
    "30-day readmission count": (
        "SELECT count(DISTINCT a.patient_id) AS n FROM gold_encounter a JOIN gold_encounter b "
        "ON a.patient_id = b.patient_id AND b.encounter_id <> a.encounter_id "
        "AND CAST(b.start_date AS DATE) > CAST(a.end_date AS DATE) "
        "AND date_diff('day', CAST(a.end_date AS DATE), CAST(b.start_date AS DATE)) <= 30 "
        "WHERE a.class_code = 'IMP'"),
    "30-day readmission with BETWEEN and INTERVAL": (
        "SELECT count(DISTINCT a.patient_id) AS n FROM gold_encounter a JOIN gold_encounter b "
        "ON a.patient_id = b.patient_id AND CAST(b.start_date AS DATE) BETWEEN "
        "CAST(a.end_date AS DATE) AND CAST(a.end_date AS DATE) + INTERVAL 30 DAY"),
    "average days from onset to abatement": (
        "SELECT round(avg(date_diff('day', CAST(onset_date AS DATE), CAST(abatement_date AS DATE))), 1) "
        "AS d FROM gold_condition WHERE abatement_date IS NOT NULL"),
    "a condition after an encounter": (
        "SELECT count(DISTINCT c.patient_id) AS n FROM gold_condition c JOIN gold_encounter e "
        "ON c.patient_id = e.patient_id WHERE CAST(c.onset_date AS DATE) >= CAST(e.start_date AS DATE)"),
    "a scalar count in WHERE": ("SELECT count(*) AS n FROM gold_encounter WHERE length_of_stay_days > "
                                "(SELECT avg(length_of_stay_days) FROM gold_encounter)"),
    "EXISTS on the key": ("SELECT count(*) AS n FROM gold_patient p WHERE EXISTS "
                          "(SELECT 1 FROM gold_condition c WHERE c.patient_id = p.patient_id "
                          "AND c.condition_name = 'Prediabetes')"),
    "stddev of per-patient values": (
        "SELECT round(stddev(t.n), 1) AS s FROM (SELECT e.patient_id, count(*) AS n "
        "FROM gold_encounter e GROUP BY e.patient_id) t"),
    "ORDER BY a count alias": ("SELECT condition_name, count(*) AS n FROM gold_condition "
                               "GROUP BY condition_name ORDER BY n DESC"),
    "a per-patient CTE joined back on the key": (
        "WITH x AS (SELECT patient_id, count(*) AS c FROM gold_encounter GROUP BY patient_id) "
        "SELECT p.gender, round(avg(x.c), 1) AS v FROM x JOIN gold_patient p "
        "ON p.patient_id = x.patient_id GROUP BY p.gender"),
    "per-patient join of two tables on the key": (
        "SELECT avg(k) AS a FROM (SELECT c.patient_id, count(DISTINCT e.encounter_id) AS k "
        "FROM gold_condition c JOIN gold_encounter e ON e.patient_id = c.patient_id "
        "GROUP BY c.patient_id) d"),
    # grouping by patient inside an aggregate (decision 4)
    "average per patient": ("WITH x AS (SELECT patient_id, count(*) AS c FROM gold_encounter "
                            "GROUP BY patient_id) SELECT round(avg(c), 1) AS v FROM x"),
    "count of patients over a threshold": (
        "SELECT count(*) AS n FROM (SELECT patient_id FROM gold_condition WHERE is_active "
        "GROUP BY patient_id HAVING count(DISTINCT condition_name) >= 5) t"),
    "distribution of a per-patient count": (
        "WITH x AS (SELECT patient_id, count(*) AS c FROM gold_encounter GROUP BY patient_id) "
        "SELECT c, count(*) AS n FROM x GROUP BY c"),
}


@pytest.mark.parametrize("name", sorted(BLOCKED))
def test_an_escape_route_is_blocked(name):
    d = validate_query(BLOCKED[name])
    assert not d.allowed, name
    if name.startswith(("query()", "query_table()")):
        # since #37 sql_guard refuses these SQL-string runners itself, earlier
        assert "not allowed" in d.reason or "disallowed" in d.reason, d.reason
    else:
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


def test_every_fhir_eval_answer_is_reachable_through_the_guard():
    """Every FHIR case's reference answer (computed on the raw backend) is what
    the guard returns for that SQL, unsuppressed: no case asks for something the
    PHI rules withhold. (The 7 that did were rewritten by the owner's decision.)"""
    real = os.path.join(os.path.dirname(__file__), "..", "..", "medallion", "fhir_gold.duckdb")
    if not os.path.exists(real):
        pytest.skip("FHIR Gold not built")
    import eval_bank_fhir
    t = AnalyticsQueryTool(db_path=real, seed_demo=False)
    for case in eval_bank_fhir.CASES:
        ref = [tuple(r) for r in t._con.execute(case["reference_sql"]).fetchall()]
        r = t.execute({"sql": case["reference_sql"]})
        assert r.success, (case["id"], r.error)
        assert [tuple(x.values()) for x in r.output["rows"]] == ref, case["id"]
        assert "suppressed" not in (r.output.get("phi_note") or ""), case["id"]


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


def test_the_value_check_looks_inside_lists_and_structs():
    d = Decision(True, "allowed", phi_sql="SELECT gender AS g FROM gold_patient", phi_names=["g"])
    for v in (["x", "2019-03-05"], {"k": "02134"}, [{"d": "1980-01-02"}]):
        with pytest.raises(phi_guard.OutputRefused):
            phi_guard.check_output(d, ["g"], [{"g": v}])
    phi_guard.check_output(d, ["g"], [{"g": ["female", "male"]}])


def test_a_per_patient_aggregate_answers_over_all_its_patients(tool):
    # 15 patients, 19 conditions: the average is over 15 people, so it is returned
    r = tool.execute({"sql": "WITH x AS (SELECT patient_id, count(*) AS c FROM gold_condition "
                             "GROUP BY patient_id) SELECT round(avg(c), 2) AS v FROM x"})
    assert r.success and r.output["rows"] == [{"v": 1.27}], (r.error, r.output)


def test_the_outer_query_counts_the_per_patient_rows_under_the_11_rule(tool):
    # 4 patients have 2 conditions: that bucket is under 11 and is suppressed;
    # the inner groups (one patient each) are not, or nothing would survive
    r = tool.execute({"sql": "WITH x AS (SELECT patient_id, count(*) AS c FROM gold_condition "
                             "GROUP BY patient_id) SELECT c, count(*) AS n FROM x GROUP BY c ORDER BY c"})
    assert r.success and r.output["rows"] == [{"c": 1, "n": 11}], (r.error, r.output)
    few = tool.execute({"sql": "SELECT count(*) AS n FROM (SELECT patient_id FROM gold_condition "
                               "GROUP BY patient_id HAVING count(*) >= 2) t"})
    assert few.success and few.output["rows"] == []


def test_passing_the_patient_column_through_a_per_patient_group_is_refused(tool):
    r = tool.execute({"sql": "WITH x AS (SELECT patient_id, count(*) AS c FROM gold_condition "
                             "GROUP BY patient_id) SELECT patient_id, c FROM x"})
    assert not r.success and "PHI" in r.error and "p0" not in r.error


def test_a_filter_on_an_identifier_is_refused_and_a_year_filter_runs(tool):
    bad = tool.execute({"sql": "SELECT count(*) AS n FROM gold_patient WHERE patient_id = 'p0'"})
    assert not bad.success and "used to filter" in bad.error
    ok = tool.execute({"sql": "SELECT count(*) AS n FROM gold_patient "
                              "WHERE YEAR(CAST(birth_date AS DATE)) > 1000"})
    assert ok.success and ok.output["rows"] == [{"n": 15}], (ok.error, ok.output)


def test_an_array_over_grouped_results_still_runs(tool):
    r = tool.execute({"sql": "SELECT ARRAY(SELECT gender FROM gold_patient GROUP BY gender) AS a"})
    assert r.success, r.error

