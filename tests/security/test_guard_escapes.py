"""Two guard-wide escapes found by adversarial testing (2026-10-30).

Both predate the PHI rules and work on ANY table in the database file:

  * query('<sql>') runs a string as SQL, so the guard never parses what it
    reads -- bronze_*, the catalog, local files. query_table('<name>') hides
    the table name the allowlist checks. Both are now denied functions.
  * A CTE named after a real table: inside the CTE's own body (or an earlier
    CTE of the same WITH) the name resolves to the REAL table, but the old rule
    exempted every CTE name everywhere. The allowlist now exempts a name only
    where it can actually resolve to the CTE.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from sql_guard import validate_query

ESCAPES = {
    "query() over bronze": "SELECT * FROM query('SELECT * FROM bronze_patient')",
    "query() over the catalog": "SELECT table_name FROM query('SELECT table_name FROM duckdb_tables()')",
    "query() reading a file": "SELECT length(content) AS n FROM query('SELECT content FROM read_text(''x'')')",
    "query_table()": "SELECT * FROM query_table('bronze_patient')",
    "query_table() of a list": "SELECT * FROM query_table(['bronze_patient'])",
    "a CTE shadowing the table it reads": ("WITH bronze_patient AS (SELECT * FROM bronze_patient) "
                                          "SELECT * FROM bronze_patient"),
    "a CTE referencing a later CTE's name": ("WITH a AS (SELECT * FROM bronze_x), bronze_x AS "
                                            "(SELECT 1 AS x) SELECT * FROM a"),
    # second review
    "json_execute_serialized_sql runs a string": (
        "SELECT count(*) AS n FROM json_execute_serialized_sql(json_serialize_sql('SELECT * FROM bronze_patient'))"),
    # third review
    "json_serialize_plan binds a string": "SELECT json_serialize_plan('SELECT * FROM bronze_patient') AS p",
    "json_serialize_plan with optimize": (
        "SELECT json_serialize_plan('SELECT * FROM bronze_patient WHERE 1 = 0', optimize := true) AS p"),
    "WITH RECURSIVE with no UNION reads the real table": (
        "WITH RECURSIVE bronze_patient AS (SELECT * FROM bronze_patient) SELECT * FROM bronze_patient"),
    "WITH RECURSIVE: the anchor reads the real table": (
        "WITH RECURSIVE bronze_patient AS (SELECT count(*) AS n FROM bronze_patient UNION SELECT 0) "
        "SELECT max(n) AS n FROM bronze_patient"),
    "WITH RECURSIVE: a non-recursive sibling": (
        "WITH RECURSIVE x AS (SELECT 1 AS n), bronze_patient AS (SELECT * FROM bronze_patient) "
        "SELECT count(*) AS n FROM bronze_patient"),
    "read_ndjson": "SELECT * FROM read_ndjson('x.json')",
    "parquet_scan": "SELECT * FROM parquet_scan('x.parquet')",
    "parquet_metadata": "SELECT * FROM parquet_metadata('x.parquet')",
    "sniff_csv": "SELECT * FROM sniff_csv('x.csv')",
    "sqlite_scan": "SELECT * FROM sqlite_scan('x.db', 't')",
    "pragma_storage_info (PHI min/max)": "SELECT * FROM pragma_storage_info('bronze_patient')",
    "pragma_show": "SELECT * FROM pragma_show('bronze_patient')",
    "checkpoint() writes": "SELECT * FROM checkpoint()",
    "current_setting": "SELECT current_setting('home_directory') AS h",
    "getvariable": "SELECT getvariable('x') AS v",
    "an unknown table function": "SELECT * FROM some_future_reader('x')",
    "a nested shadowing CTE": ("SELECT * FROM (WITH raw_member_phi AS (SELECT * FROM raw_member_phi) "
                               "SELECT * FROM raw_member_phi) t"),
}

STILL_FINE = {
    "a CTE": "WITH g AS (SELECT * FROM gold_hospital_profile) SELECT * FROM g",
    "chained CTEs": ("WITH g AS (SELECT state FROM gold_hospital_profile), h AS (SELECT * FROM g) "
                     "SELECT * FROM h"),
    "a recursive CTE": ("WITH RECURSIVE r AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM r WHERE n < 3) "
                        "SELECT * FROM r"),
    "a nested WITH": "SELECT * FROM (WITH g AS (SELECT * FROM gold_hospital_profile) SELECT * FROM g) x",
    "a recursive CTE whose recursive term names itself": (
        "WITH RECURSIVE bronze_patient AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM bronze_patient "
        "WHERE n < 3) SELECT * FROM bronze_patient"),
    "a generator in FROM": "SELECT count(*) AS n FROM range(3)",
    "a CTE named after the allowlisted table it reads": (
        "WITH gold_hospital_profile AS (SELECT * FROM gold_hospital_profile) "
        "SELECT * FROM gold_hospital_profile"),
}


@pytest.mark.parametrize("name", sorted(ESCAPES))
def test_escape_is_denied(name):
    assert not validate_query(ESCAPES[name]).allowed, name


@pytest.mark.parametrize("name", sorted(STILL_FINE))
def test_legitimate_cte_still_passes(name):
    d = validate_query(STILL_FINE[name])
    assert d.allowed, (name, d.reason)
