"""scheduled_runs.py: standing questions, classified and hashed, within cost caps.

No LLM and no database: the agent is a stub with the shape of
GovernedAgentGraph (run(), planner, max_attempts), and each case carries the
reference rows the runner would otherwise compute from Gold. What is under test
is the classification (ok / drift / failed / provider_error / skipped), the
caps, the cadence, the exit code and the issue decisions -- plus the guarantee
that schedules.yaml only names questions that are already eval cases.
"""

import datetime as dt
import json
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import scheduled_runs as sr
from eval_harness import _is_provider_error

NOW = dt.datetime(2026, 11, 2, 9, 0, tzinfo=dt.timezone.utc)

SCALAR = {"id": "t1_avg_ami_readmit", "question": "avg AMI?", "mode": "scalar",
          "_ref_rows": [{"avg_ami": 14.47}]}
KEYED = {"id": "t3_ami_watchlist_top10", "question": "worst AMI?", "mode": "keyed",
         "key_columns": ["facility_id"],
         "_ref_rows": [{"facility_id": "330059"}, {"facility_id": "330196"}]}
CASES = {c["id"]: c for c in (SCALAR, KEYED)}


def config(*schedules, budget=0.05):
    return {"budget_usd": budget, "schedules": list(schedules)}


def sched(name, case, cadence="weekly", cap=0.01, dataset="hospital"):
    return {"name": name, "case": case, "dataset": dataset, "cadence": cadence,
            "max_cost_usd": cap}


class StubAgent:
    """Answers by question, from a dict of state dicts or exceptions."""

    def __init__(self, answers, provider="gemini", model="gemini-flash-lite-latest"):
        self.answers = answers
        self.planner = SimpleNamespace(provider=provider, _model=model)
        self.max_attempts = 3
        self.calls = 0

    def run(self, question):
        self.calls += 1
        a = self.answers[question]
        if isinstance(a, Exception):
            raise a
        return {"trace_id": f"t{self.calls}", "attempts": 1, "total_cost_usd": 0.0004, **a}


def answered(rows):
    return {"allowed": True, "decided_by": "executed", "rows": rows}


class ProviderDown(Exception):
    status_code = 429


@pytest.fixture
def store(tmp_path):
    return sr.ResultStore(str(tmp_path / "scheduled.sqlite"))


def run(agent, store, *schedules, now=NOW, run_all=False, budget=0.05):
    return sr.run_schedules(config(*schedules, budget=budget), CASES, agent, store,
                            _is_provider_error, now=now, run_all=run_all)


# --- schedules.yaml ----------------------------------------------------------

def test_the_shipped_schedules_are_all_eval_cases():
    cfg = sr.load_schedules()
    assert cfg["schedules"], "schedules.yaml has standing questions"
    for s in cfg["schedules"]:
        assert s["case"] in sr.bank_cases(s["dataset"])


def test_the_shipped_caps_admit_a_gemini_run():
    # the weekly job runs on Gemini; a cap below its worst case would skip
    # every question, every week
    cfg = sr.load_schedules()
    worst = sr.worst_case_usd(SimpleNamespace(provider="gemini",
                                              _model="gemini-flash-lite-latest"), 3)
    assert all(worst <= s["max_cost_usd"] for s in cfg["schedules"])
    assert worst * len(cfg["schedules"]) <= cfg["budget_usd"]


@pytest.mark.parametrize("bad, message", [
    ("budget_usd: 0.05\nschedules:\n  - {name: a, case: no_such_case, dataset: hospital,"
     " cadence: weekly, max_cost_usd: 0.01}\n", "not in the hospital eval bank"),
    ("budget_usd: 0.05\nschedules:\n  - {name: a, case: t1_count_all, dataset: hospital,"
     " cadence: hourly, max_cost_usd: 0.01}\n", "cadence"),
    ("budget_usd: 0.05\nschedules:\n  - {name: a, case: t1_count_all, dataset: hospital,"
     " cadence: weekly, max_cost_usd: 0}\n", "max_cost_usd"),
    ("budget_usd: 0.05\nschedules:\n  - {name: a, case: t1_count_all, dataset: hospital,"
     " cadence: weekly, max_cost_usd: 0.01}\n  - {name: a, case: t1_count_5star,"
     " dataset: hospital, cadence: weekly, max_cost_usd: 0.01}\n", "duplicate"),
    ("schedules:\n  - {name: a, case: t1_count_all, dataset: hospital, cadence: weekly,"
     " max_cost_usd: 0.01}\n", "budget_usd"),
    # a FHIR case id is not a hospital case
    ("budget_usd: 0.05\nschedules:\n  - {name: a, case: f1_count_patients, dataset: hospital,"
     " cadence: weekly, max_cost_usd: 0.01}\n", "not in the hospital eval bank"),
])
def test_a_bad_schedule_file_is_refused(tmp_path, bad, message):
    p = tmp_path / "schedules.yaml"
    p.write_text(bad)
    with pytest.raises(sr.ScheduleError, match=message):
        sr.load_schedules(str(p))


# --- classification ----------------------------------------------------------

def test_first_run_is_ok_and_records_a_hash(store):
    agent = StubAgent({"avg AMI?": answered([{"avg": 14.47}])})
    [r] = run(agent, store, sched("ami", SCALAR["id"]))
    assert r["status"] == sr.OK and r["prev_hash"] is None and r["result_hash"]
    assert store.last_hash("ami") == r["result_hash"]


def test_the_same_answer_next_week_is_ok_even_if_typed_or_aliased_differently(store):
    run(StubAgent({"avg AMI?": answered([{"avg": 14.47}])}), store, sched("ami", SCALAR["id"]))
    later = NOW + dt.timedelta(days=7)
    [r] = run(StubAgent({"avg AMI?": answered([{"x": 14.470}])}), store,
              sched("ami", SCALAR["id"]), now=later)
    assert r["status"] == sr.OK


def test_a_changed_answer_that_is_still_correct_is_drift(store):
    run(StubAgent({"worst AMI?": answered(KEYED["_ref_rows"])}), store,
        sched("watch", KEYED["id"]))
    # the data moved (a CMS refresh): reference and agent agree on a new answer
    moved = [{"facility_id": "330196"}, {"facility_id": "330059"}]
    cases = {**CASES, KEYED["id"]: {**KEYED, "_ref_rows": moved}}
    [r] = sr.run_schedules(config(sched("watch", KEYED["id"])), cases,
                           StubAgent({"worst AMI?": answered(moved)}), store,
                           _is_provider_error, now=NOW + dt.timedelta(days=7))
    assert r["status"] == sr.DRIFT and r["prev_hash"] != r["result_hash"]
    assert "answer changed" in r["detail"]


def test_a_wrong_answer_fails_and_does_not_replace_the_last_good_hash(store):
    [good] = run(StubAgent({"avg AMI?": answered([{"avg": 14.47}])}), store,
                 sched("ami", SCALAR["id"]))
    [r] = run(StubAgent({"avg AMI?": answered([{"avg": 99.0}])}), store,
              sched("ami", SCALAR["id"]), now=NOW + dt.timedelta(days=7))
    assert r["status"] == sr.FAILED and r["detail"].startswith("wrong_answer")
    assert store.last_hash("ami") == good["result_hash"]


def test_a_guard_denial_fails(store):
    denied = {"allowed": False, "decided_by": "guard",
              "reason": "sql_guard: table not on Gold allowlist"}
    [r] = run(StubAgent({"avg AMI?": denied}), store, sched("ami", SCALAR["id"]))
    assert r["status"] == sr.FAILED and "allowlist" in r["detail"]


def test_a_provider_outage_is_not_a_failure(store):
    [r] = run(StubAgent({"avg AMI?": ProviderDown("Error code: 429")}), store,
              sched("ami", SCALAR["id"]))
    assert r["status"] == sr.PROVIDER_ERROR
    assert sr.exit_code([r]) == sr.EXIT_PROVIDER_UNAVAILABLE


def test_a_model_error_is_a_failure(store):
    [r] = run(StubAgent({"avg AMI?": ValueError("bad request")}), store,
              sched("ami", SCALAR["id"]))
    assert r["status"] == sr.FAILED


# --- caps ----------------------------------------------------------------------

def test_a_schedule_whose_worst_case_exceeds_its_cap_is_never_started(store):
    agent = StubAgent({"avg AMI?": answered([{"avg": 14.47}])})
    [r] = run(agent, store, sched("ami", SCALAR["id"], cap=0.0001))
    assert r["status"] == sr.SKIPPED and agent.calls == 0
    assert sr.exit_code([r]) == sr.EXIT_BUDGET


def test_the_sweep_budget_stops_before_it_can_be_exceeded(store):
    agent = StubAgent({"avg AMI?": answered([{"avg": 14.47}]),
                       "worst AMI?": answered(KEYED["_ref_rows"])})
    worst = sr.worst_case_usd(agent.planner, agent.max_attempts)
    # Spent is what the first run actually cost (0.0004); the second starts only
    # if spent + its worst case still fits. Leave room for the first alone.
    results = run(agent, store, sched("ami", SCALAR["id"]), sched("watch", KEYED["id"]),
                  budget=worst + 0.0002)
    assert [r["status"] for r in results] == [sr.OK, sr.SKIPPED]
    assert agent.calls == 1


def test_an_unlisted_expensive_model_is_capped_not_waved_through(store):
    # rate_for prices an unknown Claude model at the dearest listed rate
    agent = StubAgent({"avg AMI?": answered([{"avg": 14.47}])},
                      provider="anthropic", model="claude-some-future-model")
    [r] = run(agent, store, sched("ami", SCALAR["id"], cap=0.01))
    assert r["status"] == sr.SKIPPED and agent.calls == 0


# --- cadence -----------------------------------------------------------------------

def test_cadence(store):
    agent = StubAgent({"avg AMI?": answered([{"avg": 14.47}])})
    weekly, monthly = sched("ami", SCALAR["id"]), sched("ami-m", SCALAR["id"], "monthly")
    assert len(run(agent, store, weekly, monthly)) == 2
    # next week: weekly due, monthly not
    week = NOW + dt.timedelta(days=7)
    assert [r["schedule"] for r in run(agent, store, weekly, monthly, now=week)] == ["ami"]
    # a cron that fires a few hours early still counts as due
    early = week + dt.timedelta(days=7) - dt.timedelta(hours=3)
    assert [r["schedule"] for r in run(agent, store, weekly, monthly, now=early)] == ["ami"]
    # four weeks on, the monthly one is due again
    month = NOW + dt.timedelta(days=28)
    assert {r["schedule"] for r in run(agent, store, weekly, monthly, now=month)} == {"ami", "ami-m"}
    # --all ignores cadence
    assert len(run(agent, store, weekly, monthly, now=month, run_all=True)) == 2


def test_an_outage_does_not_reset_the_cadence(store):
    monthly = sched("ami-m", SCALAR["id"], "monthly")
    [r] = run(StubAgent({"avg AMI?": ProviderDown("Error code: 503")}), store, monthly)
    assert r["status"] == sr.PROVIDER_ERROR
    # still due next week: the outage got no answer judged
    again = run(StubAgent({"avg AMI?": answered([{"avg": 14.47}])}), store, monthly,
                now=NOW + dt.timedelta(days=7))
    assert [x["status"] for x in again] == [sr.OK]


def test_only_the_requested_dataset_runs(store):
    agent = StubAgent({"avg AMI?": answered([{"avg": 14.47}])})
    results = run(agent, store, sched("ami", SCALAR["id"]),
                  sched("fhir-one", "f1_count_patients", dataset="fhir"))
    assert [r["schedule"] for r in results] == ["ami"]


# --- exit code and issues ------------------------------------------------------------

def test_exit_code_precedence():
    r = lambda s: {"status": s}
    assert sr.exit_code([]) == sr.EXIT_OK
    assert sr.exit_code([r(sr.OK), r(sr.DRIFT)]) == sr.EXIT_OK
    assert sr.exit_code([r(sr.PROVIDER_ERROR), r(sr.SKIPPED)]) == sr.EXIT_BUDGET
    assert sr.exit_code([r(sr.SKIPPED), r(sr.FAILED)]) == sr.EXIT_FAILED


def _result(name, status):
    return {"schedule": name, "status": status, "run_at": "2026-11-02T09:00:00+00:00",
            "case": "c", "dataset": "hospital", "detail": "d", "trace_id": "t",
            "attempts": 1, "cost_usd": 0.0}


def test_issue_actions():
    t = lambda name: sr.ISSUE_PREFIX + name
    results = [_result("new-fail", sr.FAILED), _result("still-failing", sr.FAILED),
               _result("moved", sr.DRIFT), _result("recovered", sr.OK),
               _result("fine", sr.OK), _result("down", sr.PROVIDER_ERROR),
               _result("capped", sr.SKIPPED)]
    open_issues = {t("still-failing"): 11, t("recovered"): 12, t("down"): 13}
    acts = sr.issue_actions(results, open_issues, run_url="https://example/run/1")
    assert [(a["action"], a.get("title") or a["number"]) for a in acts] == [
        ("open", t("new-fail")), ("comment", 11), ("open", t("moved")), ("close", 12)]
    assert all("https://example/run/1" in a["body"] for a in acts)


def test_the_store_never_holds_rows(store):
    run(StubAgent({"worst AMI?": answered(KEYED["_ref_rows"])}), store,
        sched("watch", KEYED["id"]))
    import sqlite3
    con = sqlite3.connect(store.path)
    dump = "\n".join(con.iterdump())
    con.close()
    assert "330059" not in dump


def test_fhir_answer_values_never_reach_the_detail(store):
    # the detail goes into a public GitHub issue; FHIR answers are patient-level
    wrong = [{"patient_id": "p-123"}]
    case = {"id": "f_k", "question": "q?", "mode": "keyed", "key_columns": ["patient_id"],
            "_ref_rows": [{"patient_id": "p-999"}]}
    [r] = sr.run_schedules(config(sched("f", "f_k", dataset="fhir")), {"f_k": case},
                           StubAgent({"q?": answered(wrong)}), store, _is_provider_error,
                           now=NOW, dataset="fhir")
    assert r["status"] == sr.FAILED
    assert r["detail"] == "failed (wrong_answer): values withheld"


def test_a_correct_fhir_scalar_does_not_reach_the_detail(store):
    case = {"id": "f_s", "question": "how many?", "mode": "scalar",
            "_ref_rows": [{"n": 42}]}
    [r] = sr.run_schedules(config(sched("f", "f_s", dataset="fhir")), {"f_s": case},
                           StubAgent({"how many?": answered([{"n": 42}])}), store,
                           _is_provider_error, now=NOW, dataset="fhir")
    assert r["status"] == sr.OK
    assert r["detail"] == "ok: values withheld" and "42" not in r["detail"]


def test_a_run_that_raises_is_charged_its_worst_case(store):
    # it may have paid for several attempts before raising; the budget must
    # not treat it as free
    agent = StubAgent({"avg AMI?": ProviderDown("Error code: 429"),
                       "worst AMI?": answered(KEYED["_ref_rows"])})
    worst = sr.worst_case_usd(agent.planner, agent.max_attempts)
    results = run(agent, store, sched("ami", SCALAR["id"]), sched("watch", KEYED["id"]),
                  budget=worst * 1.5)
    assert results[0]["cost_usd"] == worst
    assert [r["status"] for r in results] == [sr.PROVIDER_ERROR, sr.SKIPPED]



def test_an_open_issue_is_found_even_if_its_label_was_removed(tmp_path, monkeypatch):
    # The review finding: issues were listed by label, so an open issue whose
    # label was removed by hand was missed and the next failure opened a duplicate.
    title = sr.ISSUE_PREFIX + "still-failing"
    calls = []

    def fake_gh(*args):
        calls.append(args)
        if args[:2] == ("issue", "list"):
            assert "--label" not in args          # listed by title, not label
            return json.dumps([{"number": 7, "title": title},       # label removed (oldest)
                               {"number": 9, "title": title},       # a newer duplicate
                               {"number": 3, "title": "unrelated"}])
        return ""
    monkeypatch.setattr(sr, "_gh", fake_gh)
    summary = tmp_path / "s.json"
    summary.write_text(json.dumps({"results": [_result("still-failing", sr.FAILED)]}))
    sr.file_issues(str(summary))
    acted = [a for a in calls if a[0] == "issue" and a[1] != "list"]
    assert acted == [("issue", "comment", "7", "--body", acted[0][4])]   # no "create"


def test_open_issues_by_title_ignores_other_issues_and_keeps_the_oldest():
    p = sr.ISSUE_PREFIX
    assert sr.open_issues_by_title([
        {"number": 20, "title": p + "a"}, {"number": 4, "title": p + "a"},
        {"number": 5, "title": "scheduled runs are great"}, {"number": 6, "title": p + "b"},
    ]) == {p + "a": 4, p + "b": 6}
