"""The eval report keeps, per run, the SQL the agent wrote and the reason it was
refused -- and never result rows (the repo is public). Owner's request
2026-11-02, after a CI run whose guard refusals could not be explained from
the report."""

from types import SimpleNamespace

import eval_harness as H


class _Planner:
    def __init__(self, sql):
        self.sql = sql

    def generate_sql(self, question):
        return self.sql


class _Runtime:
    def __init__(self, result):
        self.result = result

    def execute_tool(self, capability, params):
        return self.result


CASE = {
    "id": "c",
    "question": "q",
    "reference_sql": "SELECT 1",
    "mode": "scalar",
    "_ref_rows": [{"n": 4168}],
}


def test_a_refused_run_keeps_its_sql_and_reason():
    refused = SimpleNamespace(
        allowed=False,
        result=None,
        explanation="Operation denied: x\nReason: no such column: gold_condition.is_experiment",
    )
    sql = "SELECT count(*) FROM gold_condition WHERE is_experiment = FALSE"
    mode, _ = H.run_once(CASE, _Runtime(refused), _Planner(sql))
    assert mode == H.F_GUARD_DENIED
    assert H._LAST_RUN == {"sql": sql, "reason": refused.explanation}


def test_a_run_that_executed_keeps_its_sql_and_no_rows():
    ok = SimpleNamespace(
        allowed=True,
        explanation="",
        result=SimpleNamespace(success=True, error=None, output={"rows": [{"n": 4168}]}),
    )
    sql = "SELECT count(*) AS n FROM gold_condition WHERE is_active"
    H.run_once(CASE, _Runtime(ok), _Planner(sql))
    assert H._LAST_RUN == {"sql": sql, "reason": None}
    assert "4168" not in repr(H._LAST_RUN)  # the answer is not recorded


def test_a_long_reason_is_cut():
    refused = SimpleNamespace(allowed=False, result=None, explanation="x" * 5000)
    H.run_once(CASE, _Runtime(refused), _Planner("SELECT 1"))
    assert len(H._LAST_RUN["reason"]) == H._REASON_CHARS
