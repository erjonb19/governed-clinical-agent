"""Per-statement timeout (twelfth review, case 6; default 10 s).

The guard allows a cross join or self-join of a patient table, and nothing
used to stop the database working on it. Measured at the real Gold's size, a
three-way self-join and a cross join were still running at 60 s
(docs/measurements/compute_bomb_2026-11-03.txt). AnalyticsQueryTool now stops
any statement after QUERY_TIMEOUT_SEC, and Databricks gets STATEMENT_TIMEOUT.
"""

import os
import sys
import time

import duckdb
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import analytics_query_tool as aqt
from analytics_query_tool import AnalyticsQueryTool
from sql_guard import validate_query
from tests.security.fhir_fixture import build

# guard-ALLOWED and slow: a three-way self-join of a patient table
BOMB = ("SELECT count(*) AS n FROM gold_observation a JOIN gold_observation b "
        "ON a.patient_id = b.patient_id JOIN gold_observation c ON b.patient_id = c.patient_id")
FAST = "SELECT count(*) AS n FROM gold_observation"


@pytest.fixture
def tool(tmp_path, monkeypatch):
    monkeypatch.setenv("QUERY_TIMEOUT_SEC", "0.5")
    path = build(str(tmp_path / "fhir.duckdb"))
    con = duckdb.connect(path)
    # 40,000 observations over 20 patients: the three-way join is 1.6e9 rows
    con.execute("""CREATE TABLE gold_observation AS
        SELECT 'o' || i AS observation_id, 'p' || (i % 20) AS patient_id, 'e' AS encounter_id,
               'vital-signs' AS category, 'LOINC' AS code_system, '1' AS code, 'M' AS measure_name,
               1.0 AS value_num, 'u' AS value_unit, '2020-01-01' AS effective_date,
               'female' AS gender, 'MA' AS state, 50 AS age_at_observation
        FROM range(40000) t(i)""")
    con.close()
    return AnalyticsQueryTool(db_path=path, seed_demo=False)


def test_the_bomb_is_allowed_by_the_guard():
    # the reason a timeout is needed: this is not a PHI question
    assert validate_query(BOMB).allowed


def test_a_slow_statement_is_stopped_with_a_clear_error(tool):
    t0 = time.perf_counter()
    r = tool.execute({"sql": BOMB})
    took = time.perf_counter() - t0
    assert not r.success
    assert "timed out after 0.5 s" in r.error and "QUERY_TIMEOUT_SEC" in r.error
    assert took < 5, took                    # stopped, not run to completion


def test_the_connection_works_after_a_timeout(tool):
    assert not tool.execute({"sql": BOMB}).success
    r = tool.execute({"sql": FAST})
    assert r.success and r.output["rows"] == [{"n": 40000}]


def test_a_fast_query_is_untouched_and_its_timer_never_fires_later(tool):
    assert tool.execute({"sql": FAST}).success
    time.sleep(0.8)                          # past the 0.5 s timer of the first query
    r = tool.execute({"sql": FAST})
    assert r.success and r.output["rows"] == [{"n": 40000}]


def test_an_interrupt_on_an_idle_connection_does_not_reach_the_next_query():
    # the race run_with_timeout relies on: a timer that fires just after a
    # query finished must not kill the next one (DuckDB behaviour, pinned here)
    con = duckdb.connect()
    con.interrupt()
    assert con.execute("SELECT 42").fetchall() == [(42,)]


def test_a_user_interrupt_is_not_reported_as_a_timeout():
    con = duckdb.connect()
    import threading
    threading.Timer(0.2, con.interrupt).start()
    with pytest.raises(duckdb.InterruptException):
        aqt.run_with_timeout(con, "SELECT count(*) FROM range(100000000000) a", 30)


@pytest.mark.parametrize("value, expected", [
    (None, 10.0), ("3", 3.0), ("0.25", 0.25), ("0", 10.0), ("-1", 10.0), ("off", 10.0)])
def test_query_timeout_defaults_to_10_s_and_cannot_be_switched_off(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("QUERY_TIMEOUT_SEC", raising=False)
    else:
        monkeypatch.setenv("QUERY_TIMEOUT_SEC", value)
    assert aqt.query_timeout() == expected


def test_databricks_gets_the_same_limit_as_statement_timeout(monkeypatch):
    import data_backends
    for k, v in {"DATABRICKS_SERVER_HOSTNAME": "h", "DATABRICKS_HTTP_PATH": "p",
                 "DATABRICKS_TOKEN": "t", "QUERY_TIMEOUT_SEC": "7"}.items():
        monkeypatch.setenv(k, v)
    seen = {}

    class Cur:
        description = [("n",)]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql):
            pass

        def fetchall(self):
            return [(1,)]

    class Conn(Cur):
        def cursor(self):
            return Cur()

    def connect(**kw):
        seen.update(kw)
        return Conn()

    pytest.importorskip("databricks.sql")
    monkeypatch.setattr("databricks.sql.connect", connect)
    cols, rows = data_backends.DatabricksBackend().execute("SELECT 1 AS n")
    assert rows == [{"n": 1}]
    assert seen["session_configuration"] == {"STATEMENT_TIMEOUT": "7"}
