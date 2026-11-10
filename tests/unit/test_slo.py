"""Backlog item 7: SLO targets, checked weekly, one issue on a miss."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import cost_tracker  # noqa: E402
import slo  # noqa: E402


def _result(p95_1=20.0, p95_5=110.0, p99_5=130.0, ok_5=200, errors_5=None):
    def level(users, p95, p99, ok=200, errors=None):
        return {
            "users": users,
            "requests": 200,
            "ok": ok,
            "errors": errors or {},
            "p50_ms": p95 / 2,
            "p95_ms": p95,
            "p99_ms": p99,
            "throughput_rps": 50.0,
        }

    return {
        "machine": "test",
        "results": [
            level(1, p95_1, p95_1 + 5),
            level(5, p95_5, p99_5, ok_5, errors_5),
            level(20, 480.0, 500.0),
        ],
    }


def test_todays_measured_numbers_meet_every_target():
    assert slo.check(_result()) == []


def test_each_target_is_checked():
    assert any("1 users: p95_ms" in m for m in slo.check(_result(p95_1=151)))
    assert any("5 users: p95_ms" in m for m in slo.check(_result(p95_5=401)))
    assert any("5 users: p99_ms" in m for m in slo.check(_result(p99_5=601)))


def test_failed_requests_are_a_miss_at_any_level():
    misses = slo.check(_result(ok_5=199, errors_5={"429": 1}))
    assert any("failed requests" in m for m in misses)


def test_a_level_that_was_not_measured_is_a_miss():
    r = _result()
    r["results"] = [x for x in r["results"] if x["users"] != 5]
    assert any("5 users: not measured" in m for m in slo.check(r))


def test_issue_opens_comments_and_closes():
    miss = slo.check(_result(p95_5=900))
    assert [a["action"] for a in slo.actions(miss, _result(), None)] == ["open"]
    opened = slo.actions(miss, _result(), None)[0]
    assert opened["label"] == "slo-miss" and opened["title"] == slo.TITLE
    assert [a["action"] for a in slo.actions(miss, _result(), 12)] == ["comment"]
    assert [a["action"] for a in slo.actions([], _result(), 12)] == ["close"]
    assert slo.actions([], _result(), None) == []


def test_metrics_report_p99(tmp_path):
    log = tmp_path / "m.jsonl"
    log.write_text("".join(f'{{"latency_ms": {i}}}\n' for i in range(1, 101)))
    s = cost_tracker.summarize(str(log))
    assert s["latency_ms_p50"] <= s["latency_ms_p95"] <= s["latency_ms_p99"] == 100
