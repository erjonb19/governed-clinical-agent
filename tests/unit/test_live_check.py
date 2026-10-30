"""live_check.py: when the deploy counts as up, retries, and the one-issue rule.

No browser and no network: check() takes the single-attempt function as a
parameter, so the retry loop is driven by scripted attempt results, and the
pass criteria are a pure function of what one attempt observed.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import live_check as lc

GOOD = dict(health_status=200, health_body={"status": "ok"}, ui_status=200,
            title="Clinical Intelligence — Governed Analytics", shell_ok=True,
            console_errors=[])


def test_a_healthy_load_has_no_problems():
    assert lc.problems(**GOOD) == []


def test_each_criterion_fails_on_its_own():
    cases = {
        "health_status": (503, "/health returned 503"),
        "health_body": ({"status": "degraded"}, "/health status is 'degraded'"),
        "ui_status": (502, "/ui returned 502"),
        "title": ("Service Suspended", "/ui title is 'Service Suspended'"),
        "shell_ok": (False, "app shell"),
        "console_errors": (["Failed to load resource: 401"], "console error: Failed to load"),
    }
    for field, (value, expect) in cases.items():
        found = lc.problems(**{**GOOD, field: value})
        assert len(found) == 1 and expect in found[0], (field, found)


def scripted(*results):
    calls = []

    def once(url, timeout_s, shot):
        calls.append(shot)
        return results[len(calls) - 1]
    return once, calls


def test_a_cold_start_that_recovers_passes():
    once, calls = scripted(["/health unreachable: ReadTimeout"], [])
    slept = []
    r = lc.check("http://x", attempts=3, pause_s=30, once=once, sleep=slept.append)
    assert r["ok"] and len(r["attempts"]) == 2 and slept == [30]


def test_it_fails_only_after_every_attempt_fails():
    once, calls = scripted(["a"], ["b"], ["c"])
    slept = []
    r = lc.check("http://x", attempts=3, pause_s=30, screenshot_dir="shots",
                 once=once, sleep=slept.append)
    assert not r["ok"] and [t["problems"] for t in r["attempts"]] == [["a"], ["b"], ["c"]]
    assert slept == [30, 30]   # no pause after the last attempt
    assert calls == [os.path.join("shots", f"attempt-{n}.png") for n in (1, 2, 3)]


def _fail():
    return {"ok": False, "url": "http://x",
            "attempts": [{"attempt": n, "problems": ["/health returned 503"]} for n in (1, 2, 3)]}


def test_a_failure_opens_one_issue():
    a = lc.issue_action(_fail(), open_issue=None, run_url="https://run/1")
    assert a["action"] == "open" and a["title"] == lc.ISSUE_TITLE
    assert "attempt 3: /health returned 503" in a["body"] and "https://run/1" in a["body"]


def test_a_failure_never_opens_a_duplicate():
    a = lc.issue_action(_fail(), open_issue=42)
    assert a == {"action": "comment", "number": 42, "body": a["body"]}


def test_a_pass_closes_an_open_issue_and_otherwise_does_nothing():
    ok = {"ok": True, "url": "http://x", "attempts": [{"attempt": 1, "problems": []}]}
    assert lc.issue_action(ok, open_issue=42)["action"] == "close"
    assert lc.issue_action(ok, open_issue=None) is None


def test_the_ui_does_not_probe_auth_without_a_key():
    # An empty-key /auth-check 401s into the console on every first visit, and
    # the live check fails on any console error. The startup sequence must only
    # test a key it actually has.
    with open(os.path.join(os.path.dirname(__file__), "..", "..", "web", "index.html"),
              encoding="utf-8") as f:
        html = f.read()
    startup = html[html.index('if(!cfg.auth_required)'):html.index("/* ---------- nav")]
    assert 'let ok=haveKey?await testKey(true):false' in startup
    assert startup.count("testKey(") == 1
    # and an unreachable service keeps its "Offline" badge: only an untested
    # (missing) key is labelled "Key needed"
    assert 'if(!haveKey) setConn("bad","Key needed")' in startup


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def test_a_non_json_health_page_is_not_ok_rather_than_unreachable():
    assert lc.read_health(_Resp(200, ValueError("not json"))) == (200, None)
    assert lc.problems(**{**GOOD, "health_body": None}) == ["/health status is None, not 'ok'"]
    assert lc.read_health(_Resp(200, {"status": "ok"})) == (200, {"status": "ok"})
    assert lc.read_health(_Resp(503, None)) == (503, None)


class _FakePage:
    def __init__(self):
        self.handlers, self.shots = {}, []

    def on(self, event, fn):
        self.handlers[event] = fn

    def goto(self, *a, **k):
        msg = type("M", (), {"type": "error", "text": "Failed to load resource: 502"})()
        self.handlers["console"](msg)
        raise TimeoutError("networkidle not reached")

    def screenshot(self, path, full_page):
        self.shots.append(path)


class _FakePlaywright:
    def __init__(self, page):
        self.chromium = self
        self._page = page

    def launch(self):
        return self

    def new_page(self):
        return self._page

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_a_failed_load_still_screenshots_and_keeps_the_console(monkeypatch):
    import httpx
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(200, {"status": "ok"}))
    page = _FakePage()
    found = lc.check_once("http://x", 1, "shot.png",
                          playwright_factory=lambda: _FakePlaywright(page))
    assert page.shots == ["shot.png"]
    assert found[0].startswith("/ui did not load: TimeoutError")
    assert "console error: Failed to load resource: 502" in found
