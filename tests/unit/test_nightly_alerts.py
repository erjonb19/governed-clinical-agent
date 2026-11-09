"""Backlog item 3: the nightly eval opens, updates and closes alert issues.

Two kinds, never mixed (owner, 2026-11-06): eval-regression and provider-outage,
one issue each per dataset. No network: actions() is pure; main() is checked in
--dry-run.
"""

import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import eval_harness
import nightly_alerts as na

REPORT = {"timestamp": "20261109T150000Z", "status": "fail", "completed_accuracy": 0.71,
          "completed_runs": 84, "provider_error_runs": 0, "provider": "gemini",
          "model": "gemini-flash-lite-latest",
          "cases": [{"id": "f4_x", "passed": False, "correct_runs": 0, "runs": 3},
                    {"id": "f1_y", "passed": True, "correct_runs": 3, "runs": 3}]}
OUT = {**REPORT, "status": "provider_unavailable", "completed_runs": 0,
       "provider_error_runs": 84, "cases": []}
OK = {**REPORT, "status": "pass", "completed_accuracy": 0.97, "cases": []}
REG_T, OUT_T = "nightly eval regression: fhir", "nightly provider outage: fhir"


def test_the_verdict_follows_the_gate_exit_code():
    assert na.classify(0, OK) == na.HEALTHY
    assert na.classify(1, REPORT) == na.REGRESSION
    assert na.classify(2, OUT) == na.OUTAGE
    assert na.classify(1, None) == na.REGRESSION      # the harness broke: red, investigate


def test_a_regression_opens_one_regression_issue():
    acts = na.actions("fhir", na.REGRESSION, REPORT, {})
    assert [(a["action"], a["title"], a["label"]) for a in acts] == \
        [("open", REG_T, "eval-regression")]
    body = acts[0]["body"]
    assert "71.0%" in body and "`f4_x` 0/3" in body and "f1_y" not in body


def test_an_outage_opens_an_outage_issue_and_never_a_regression():
    acts = na.actions("fhir", na.OUTAGE, OUT, {})
    assert [(a["action"], a["label"]) for a in acts] == [("open", "provider-outage")]
    assert "Not an accuracy regression" in acts[0]["body"]


def test_a_repeat_comments_instead_of_opening_a_second():
    acts = na.actions("fhir", na.REGRESSION, REPORT, {REG_T: 12})
    assert [(a["action"], a["number"]) for a in acts] == [("comment", 12)]


def test_a_healthy_night_closes_both():
    acts = na.actions("fhir", na.HEALTHY, OK, {REG_T: 12, OUT_T: 13})
    assert sorted((a["action"], a["number"]) for a in acts) == [("close", 12), ("close", 13)]


def test_an_outage_leaves_an_open_regression_alone():
    acts = na.actions("fhir", na.OUTAGE, OUT, {REG_T: 12})
    assert [a["action"] for a in acts] == ["open"] and all(a.get("number") != 12 for a in acts)


def test_a_scored_regression_closes_the_outage_issue():
    # the provider answered, so the outage is over even though the score is bad
    acts = na.actions("fhir", na.REGRESSION, REPORT, {OUT_T: 13})
    assert ("close", 13) in [(a["action"], a.get("number")) for a in acts]


def test_a_crashed_retry_after_an_outage_does_not_close_the_outage_issue():
    # rc=1 with the first attempt's outage report as the newest one
    acts = na.actions("fhir", na.REGRESSION, OUT, {OUT_T: 13})
    assert all(a["action"] != "close" for a in acts)


def test_datasets_have_their_own_issues():
    acts = na.actions("hospital", na.REGRESSION, REPORT, {REG_T: 12})
    assert acts[0]["action"] == "open" and acts[0]["title"] == "nightly eval regression: hospital"


def test_issues_are_found_by_title_oldest_first():
    listed = [{"number": 20, "title": REG_T}, {"number": 9, "title": REG_T},
              {"number": 3, "title": "something else"}]
    assert na.open_issues_by_title(listed) == {REG_T: 9}


def test_the_alert_bar_is_the_gate_bar():
    # the README states 80%; if the gate moves, this says so
    assert eval_harness.THRESHOLD == 0.80
    assert eval_harness.MIN_COMPLETED_FRACTION == 0.80


def test_dry_run_reads_the_newest_report_after_since(tmp_path):
    runs = tmp_path / "eval_runs"
    runs.mkdir()
    old = runs / "eval_single_fhir_20261108T000000Z.json"
    old.write_text(json.dumps(OK))
    os.utime(old, (1000, 1000))
    (runs / "eval_single_fhir_20261109T150000Z.json").write_text(json.dumps(REPORT))
    script = os.path.join(os.path.dirname(__file__), "..", "..", "nightly_alerts.py")
    out = subprocess.run([sys.executable, os.path.abspath(script), "--dataset", "fhir",
                          "--gate-exit", "1", "--since", "2000", "--dry-run"],
                         cwd=tmp_path, capture_output=True, text=True, check=True)
    assert "-> regression; 1 action(s)" in out.stdout and f"open: {REG_T}" in out.stdout
