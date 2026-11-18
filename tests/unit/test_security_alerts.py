"""Backlog item 59: nightly security alerts (security_alerts.py)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import auth_failures  # noqa: E402
import security_alerts as S  # noqa: E402

QUIET = {
    "auth_failures": {"total": 3, "clients_over_limit": 0, "per_window": 10, "window_sec": 300},
    "llm_budget": {
        "day": "2026-11-18",
        "runs": 10,
        "max_runs": 300,
        "est_usd": 0.01,
        "max_usd": 1.0,
    },
}


def test_a_quiet_service_fires_nothing():
    assert S.classify(QUIET) == {}


def test_a_client_over_the_limit_fires_the_auth_alert():
    m = {**QUIET, "auth_failures": {**QUIET["auth_failures"], "clients_over_limit": 2}}
    assert S.AUTH in S.classify(m)


def test_many_refused_keys_fire_the_auth_alert(monkeypatch):
    monkeypatch.setenv("SECURITY_ALERT_AUTH_TOTAL", "5")
    m = {**QUIET, "auth_failures": {**QUIET["auth_failures"], "total": 5}}
    assert S.AUTH in S.classify(m)


def test_spend_near_either_cap_fires():
    runs = {**QUIET, "llm_budget": {**QUIET["llm_budget"], "runs": 240}}
    usd = {**QUIET, "llm_budget": {**QUIET["llm_budget"], "est_usd": 0.85}}
    assert S.SPEND in S.classify(runs) and S.SPEND in S.classify(usd)


def test_open_comment_and_close():
    title = S.TITLES[S.AUTH]
    assert S.actions({S.AUTH: "x"}, {})[0]["action"] == "open"
    assert S.actions({S.AUTH: "x"}, {title: 7}) == [
        {"action": "comment", "number": 7, "body": "x."}
    ]
    assert S.actions({}, {title: 7})[0] == {"action": "close", "number": 7, "body": "Quiet again."}
    assert S.actions({}, {}) == []


def test_no_secret_means_no_check(monkeypatch, capsys):
    monkeypatch.delenv("ALERT_API_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", ["security_alerts.py"])
    assert S.main() == 0
    assert "ALERT_API_KEY is not set" in capsys.readouterr().out


def test_an_unreachable_service_files_nothing(monkeypatch, capsys):
    monkeypatch.setenv("ALERT_API_KEY", "k")
    monkeypatch.setattr(sys, "argv", ["security_alerts.py"])

    def boom(key, timeout=0):
        raise OSError("down")

    def no_gh(*a):
        raise AssertionError("gh must not be called")

    monkeypatch.setattr(S, "fetch_metrics", boom)
    monkeypatch.setattr(S, "_gh", no_gh)
    assert S.main() == 0
    assert "could not read" in capsys.readouterr().out


def test_the_summary_carries_no_client_address():
    f = auth_failures.AuthFailures(per_window=2, window_sec=300)
    for _ in range(3):
        f.record("198.51.100.23", "/query", "api")
    s = f.summary()
    assert s["total"] == 3 and s["clients_over_limit"] == 1
    assert "198.51.100.23" not in repr(s)
