"""Backlog item 11: the manual FHIR eval passes when the branch scores at
least main, and never on an inconclusive run."""

import importlib.util
import os

_spec = importlib.util.spec_from_file_location(
    "compare_evals",
    os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "compare_evals.py"),
)
ce = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ce)


def _report(acc, status="pass", runs=84, cases=None):
    return {
        "completed_accuracy": acc,
        "status": status,
        "completed_runs": runs,
        "cases": cases or [{"id": "f1", "passed": True, "correct_runs": 3, "runs": 3}],
    }


def test_equal_or_better_passes():
    assert ce.compare(_report(0.95), _report(0.95))[0] == 0
    assert ce.compare(_report(0.97), _report(0.95))[0] == 0


def test_below_main_fails_even_above_the_80_percent_gate():
    code, lines = ce.compare(_report(0.93), _report(0.976))
    assert code == 1 and "below main" in lines[-1]


def test_an_inconclusive_run_on_either_side_is_not_a_pass():
    assert ce.compare(_report(1.0, "provider_unavailable", 10), _report(0.9))[0] == 2
    assert ce.compare(_report(1.0), _report(0.9, "incomplete"))[0] == 2


def test_a_missing_report_is_inconclusive():
    assert ce.compare(None, _report(0.9))[0] == 2


def test_the_table_shows_both_scores_and_flags_changed_cases():
    b = _report(0.9, cases=[{"id": "f1", "passed": False, "correct_runs": 1, "runs": 3}])
    m = _report(0.95, cases=[{"id": "f1", "passed": True, "correct_runs": 3, "runs": 3}])
    _, lines = ce.compare(b, m)
    assert "| f1 | 1/3 ▼ | 3/3 |" in lines


def test_reads_the_newest_report_in_a_directory(tmp_path):
    import json, time  # noqa: E401

    (tmp_path / "eval_single_fhir_1.json").write_text(json.dumps(_report(0.5)))
    time.sleep(0.01)
    (tmp_path / "eval_single_fhir_2.json").write_text(json.dumps(_report(0.9)))
    os.utime(tmp_path / "eval_single_fhir_2.json", None)
    assert ce.newest_report(str(tmp_path))["completed_accuracy"] == 0.9
