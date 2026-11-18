"""The live deploy behind main opens one `deploy-lag` issue (owner, 2026-11-18)."""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
import deploy_lag as D  # noqa: E402

A, B = "a" * 40, "b" * 40


def test_classify():
    assert D.classify(A, A, 600, 60) == D.HEALTHY
    assert D.classify(A, B, 10, 60) == D.PENDING  # main just moved: the deploy is building
    assert D.classify(A, B, 61, 60) == D.LAGGING
    assert D.classify("unknown", B, 600, 60) == D.UNKNOWN
    assert D.classify("", B, 600, 60) == D.UNKNOWN


def test_actions():
    assert D.actions(D.LAGGING, None, "x")[0]["action"] == "open"
    assert D.actions(D.LAGGING, 9, "x") == [{"action": "comment", "number": 9, "body": "x"}]
    assert D.actions(D.HEALTHY, 9, "")[0]["action"] == "close"
    assert D.actions(D.HEALTHY, None, "") == []
    assert D.actions(D.PENDING, 9, "") == [] and D.actions(D.UNKNOWN, 9, "") == []


def _main(monkeypatch, live, age_hours):
    import datetime as dt

    when = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=age_hours)).isoformat()
    calls = []

    def gh(*args):
        calls.append(args)
        if args[:2] == ("api", f"repos/{D.REPO}/commits/main"):
            return json.dumps({"sha": B, "commit": {"committer": {"date": when}}})
        if args[0] == "api":
            return json.dumps({"status": "ahead", "ahead_by": 3})
        if args[:2] == ("issue", "list"):
            return "[]"
        return ""

    monkeypatch.setattr(D, "_gh", gh)
    monkeypatch.setattr(D, "live_commit", lambda timeout=0: live)
    monkeypatch.setattr(sys, "argv", ["deploy_lag.py"])
    assert D.main() == 0
    return calls


def test_a_stale_deploy_opens_an_issue(monkeypatch):
    calls = _main(monkeypatch, A, 3)
    created = [c for c in calls if c[:2] == ("issue", "create")]
    assert created and "3 commit(s) ahead" in created[0][created[0].index("--body") + 1]


def test_a_fresh_main_commit_waits(monkeypatch):
    calls = _main(monkeypatch, A, 0.2)
    assert not [c for c in calls if c[0] == "issue" and c[1] != "list"]


def test_an_unreachable_service_files_nothing(monkeypatch, capsys):
    def boom(timeout=0):
        raise OSError("asleep")

    def no_gh(*a):
        raise AssertionError("gh must not run")

    monkeypatch.setattr(D, "live_commit", boom)
    monkeypatch.setattr(D, "_gh", no_gh)
    monkeypatch.setattr(sys, "argv", ["deploy_lag.py"])
    assert D.main() == 0
    assert "could not read" in capsys.readouterr().out
