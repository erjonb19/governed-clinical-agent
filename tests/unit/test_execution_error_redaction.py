"""Execution errors never carry data values (hard rule 3).

DuckDB quotes the offending value in a conversion error. Since /query runs on
the graph, that error text lands in checkpointed state and in the retry prompt
sent to the LLM provider, as well as in the response and the trace; and
gold_patient's name columns are on the allowlist. So the tool redacts every
single-quoted literal at the source, and keeps double-quoted identifiers, which
the agent needs to correct its SQL.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from analytics_query_tool import AnalyticsQueryTool, redact_values

GOLD = os.path.join(os.path.dirname(__file__), "..", "..", "medallion", "hospital_gold.duckdb")


def test_values_are_redacted_and_identifiers_kept():
    msg = ('Conversion Error: Could not convert string \'Jane O\'\'Doe\' to INT32 '
           'LINE 1: SELECT CAST("family_name" AS INT) FROM gold_patient WHERE x = \'MA\'')
    out = redact_values(msg)
    assert "Jane" not in out and "Doe" not in out and "'MA'" not in out
    assert '"family_name"' in out and "'<value>'" in out


@pytest.mark.skipif(not os.path.exists(GOLD), reason="hospital Gold not built")
def test_a_real_conversion_error_does_not_quote_the_value():
    tool = AnalyticsQueryTool(db_path=GOLD, seed_demo=False)
    name = tool.execute({"sql": "SELECT facility_name FROM gold_hospital_profile WHERE "
                                "facility_name IS NOT NULL ORDER BY facility_id LIMIT 1"}
                        ).output["rows"][0]["facility_name"]
    r = tool.execute({"sql": "SELECT CAST(facility_name AS INTEGER) AS x FROM gold_hospital_profile "
                             "WHERE facility_name IS NOT NULL ORDER BY facility_id LIMIT 1"})
    assert not r.success and "query execution failed" in r.error
    assert name not in r.error and "'<value>'" in r.error
