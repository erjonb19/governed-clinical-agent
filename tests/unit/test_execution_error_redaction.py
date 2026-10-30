"""Execution errors never carry data values (hard rule 3).

DuckDB quotes the offending VALUE in an error about data -- in single quotes, in
double quotes, or bare under a caret -- and since /query runs on the graph that
text lands in checkpointed state and in the retry prompt sent to the LLM
provider, as well as the response and the trace. The PHI at risk is FHIR's
birth_date, city and postal_code. So an error about a value is reduced to its
class and target type; an error about the SQL keeps its text (it names columns,
which the agent needs), with quoted literals redacted.
"""

import os
import sys

import duckdb
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from analytics_query_tool import AnalyticsQueryTool, describe_execution_error, redact_values

GOLD = os.path.join(os.path.dirname(__file__), "..", "..", "medallion", "hospital_gold.duckdb")

# (sql that fails on a value, the value that must not survive)
DATA_ERRORS = [
    ("SELECT strptime('1980-05-01', '%m/%d/%Y')", "1980-05-01"),       # bare + double-quoted
    ("SELECT CAST('Boston' AS DATE)", "Boston"),                          # double-quoted
    ("SELECT CAST('2020-13-45 10:00' AS TIMESTAMP)", "2020-13-45"),       # double-quoted
    ("SELECT CAST('12a' AS DECIMAL(5,2))", "12a"),                        # double-quoted
    ("SELECT CAST('O''Brien, Mary' AS INT)", "Brien"),                    # unbalanced quote
    ("SELECT CAST('02134' AS DATE)", "02134"),
]


def _error(sql):
    con = duckdb.connect()
    try:
        con.execute(sql)
    except Exception as e:
        return e
    finally:
        con.close()
    raise AssertionError(f"expected {sql!r} to fail")


@pytest.mark.parametrize("sql, value", DATA_ERRORS)
def test_an_error_about_a_value_never_contains_it(sql, value):
    e = _error(sql)
    assert value in str(e)                 # DuckDB really does quote it
    out = describe_execution_error(e)
    assert value not in out and "withheld" in out


def test_the_target_type_is_kept_so_the_agent_can_fix_its_sql():
    out = describe_execution_error(_error("SELECT CAST('12a' AS DECIMAL(5,2))"))
    assert "converting to DECIMAL(5,2)" in out


def test_an_error_about_the_sql_keeps_its_text_but_not_literals():
    out = describe_execution_error(_error("SELECT nosuchcol FROM (SELECT 'secret' AS x)"))
    assert '"nosuchcol"' in out and "secret" not in out


def test_databricks_style_messages_are_caught_by_their_words():
    class ServerOperationError(Exception):
        pass
    e = ServerOperationError("[CAST_INVALID_INPUT] The value '1980-05-01' of the type "
                             "\"STRING\" cannot be cast to \"DATE\" because it is malformed.")
    out = describe_execution_error(e)
    assert "1980-05-01" not in out and "withheld" in out


def test_redact_values_alone():
    assert redact_values("x = 'MA' and y = 'it''s'") == "x = '<value>' and y = '<value>'"


@pytest.mark.skipif(not os.path.exists(GOLD), reason="hospital Gold not built")
@pytest.mark.parametrize("expr", ["CAST(city AS DATE)", "strptime(city, '%m/%d/%Y')",
                                  "CAST(facility_name AS INTEGER)"])
def test_through_the_real_tool_no_cell_value_comes_back(expr):
    tool = AnalyticsQueryTool(db_path=GOLD, seed_demo=False)
    row = tool.execute({"sql": "SELECT city, facility_name FROM gold_hospital_profile "
                               "WHERE city IS NOT NULL ORDER BY facility_id LIMIT 1"}).output["rows"][0]
    r = tool.execute({"sql": f"SELECT {expr} AS x FROM gold_hospital_profile "
                             "WHERE city IS NOT NULL ORDER BY facility_id LIMIT 1"})
    assert not r.success and "query execution failed" in r.error
    assert row["city"] not in r.error and row["facility_name"] not in r.error


def test_a_value_that_looks_like_a_type_is_not_read_as_one():
    # DuckDB puts the value before the type; " to WORD" inside a value must not
    # be taken for the target type (it would carry the value out)
    e = _error("SELECT CAST('go to FRANKLIN' AS INT)")
    out = describe_execution_error(e)
    assert "FRANKLIN" not in out and "converting to INT32" in out


def test_a_databricks_data_exception_is_caught_by_its_sqlstate():
    class ServerOperationError(Exception):
        pass
    e = ServerOperationError("[NUMERIC_VALUE_OUT_OF_RANGE.WITH_SUGGESTION] 1980051 cannot be "
                             "represented as Decimal(4, 0). SQLSTATE: 22003")
    out = describe_execution_error(e)
    assert "1980051" not in out and "withheld" in out
