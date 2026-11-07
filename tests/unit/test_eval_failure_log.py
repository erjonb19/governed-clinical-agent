"""The eval prints each failing run's SQL into the log (owner, 2026-11-07).

The report artifact is the only other place the SQL would be, and it cannot
always be fetched; a failing case must be readable straight from the GitHub
Actions log. Correct runs and provider errors (a 429 says nothing about SQL)
are not printed.
"""

import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import eval_harness as eh  # noqa: E402


def test_only_wrong_runs_are_printed_with_their_sql():
    lines = eh.failure_log_lines(
        "f5_x",
        [eh.F_CORRECT, eh.F_GUARD_DENIED, eh.F_PROVIDER_ERROR, eh.F_WRONG_ANSWER],
        ["5.35", "PHI rule: a grouped aggregate ...", "429", "agent=590 ref=25.82"],
        ["SELECT 1", "WITH e AS (SELECT gender,\n count(*) n FROM gold_encounter) SELECT *", None,
         "SELECT count(*), avg(value_num) FROM gold_observation"])
    assert lines == [
        f"    run 2 [{eh.F_GUARD_DENIED}] PHI rule: a grouped aggregate ...",
        "      sql: WITH e AS (SELECT gender, count(*) n FROM gold_encounter) SELECT *",
        f"    run 4 [{eh.F_WRONG_ANSWER}] agent=590 ref=25.82",
        "      sql: SELECT count(*), avg(value_num) FROM gold_observation",
    ]


def test_a_run_with_no_sql_says_so():
    lines = eh.failure_log_lines("c", [eh.F_MODEL_ERROR], ["declined"], [None])
    assert lines[1].endswith("(none: no SQL was produced)")


def test_run_once_records_the_sql_it_ran():
    planner = types.SimpleNamespace(generate_sql=lambda q: "SELECT 42 AS n")

    class Denied:
        allowed, explanation = False, "DENIED by sql_guard: nope"

    runtime = types.SimpleNamespace(execute_tool=lambda cap, params: Denied())
    mode, _ = eh.run_once({"question": "q", "mode": "scalar"}, runtime, planner)
    assert mode == eh.F_GUARD_DENIED and eh.LAST_RUN_SQL["sql"] == "SELECT 42 AS n"


def test_run_once_graph_records_the_last_plan_sql():
    agent = types.SimpleNamespace(run=lambda q: {"allowed": False, "reason": "guard: x",
                                                 "sql": "SELECT 7", "attempts": 3})
    eh.run_once_graph({"question": "q"}, agent)
    assert eh.LAST_RUN_SQL["sql"] == "SELECT 7"
