"""The API and the UI end to end, in a real browser, on recorded LLM responses (PLAN v2 7a).

A real `uvicorn app:app` serves the committed hospital Gold with
PLANNER_PROVIDER=replay: every plan and critic call is answered from
tests/e2e/recordings/llm.json, a miss fails loudly, and nothing reaches a
provider. Chromium (pytest-playwright) drives the UI.

    1 ask             question -> answer, the SQL, and its trace
                      (plan -> critic -> guard -> execute)
    2 guard denial    a question whose recorded SQL reads a bronze table: the
                      refusal names the rule, and the retries are in the trace
    7 answers         the landing view (item 9): three tabs, every question
                      answers with a number, the definitions, the limits, and
                      the other views and raw controls hidden until Advanced
    8 first visit     no key saved or prefilled (the deployed case): one click
                      to a numeric answer, no failed request, no console error
    3 approval        Review: draft & pause -> pending -> approve as a named
                      reviewer -> committed, the decision in the trace
      escalation      escalate -> still pending, now owned by someone else
    5 API             401 without a key; /traces returns the run

Run with RUN_E2E=1 (tests/e2e/conftest.py). Re-record: tests/e2e/README.md.
"""

import os
import socket
import subprocess
import sys
import time

import httpx
import pytest

pytestmark = pytest.mark.e2e

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
KEY = "e2e-api-key"

ASK = "How many hospitals have an overall star rating of exactly 5?"
DENY = "Show every column of bronze_patient for the first ten patients."
PROPOSE = "Which hospitals have the worst heart failure readmission rates?"
ESCALATE = "Which hospitals have the longest emergency department waits?"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("api")
    port = _free_port()
    env = {**os.environ,
           "PLANNER_PROVIDER": "replay", "ALLOW_REPLAY": "1", "API_KEY": KEY, "QUERY_MODE": "graph",
           "PLANNER_MIN_INTERVAL_SEC": os.environ.get("PLANNER_MIN_INTERVAL_SEC", "0"),
           "TRACE_DB": str(tmp / "traces.sqlite"), "CHECKPOINT_DB": str(tmp / "cp.sqlite"),
           "APPROVALS_DB": str(tmp / "approvals.sqlite"), "AUDIT_LOG_DIR": str(tmp / "audit"),
           "PLANNER_METRICS_LOG": str(tmp / "metrics.jsonl"), "PLANNER_SCHEMA": "hospital"}
    log = open(tmp / "server.log", "w")
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app:app", "--port", str(port)],
                            cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 90
    while True:
        try:
            h = httpx.get(base + "/health", timeout=2).json()
            break
        except httpx.HTTPError:
            if proc.poll() is not None or time.time() > deadline:
                proc.kill()
                raise RuntimeError(open(tmp / "server.log").read())
            time.sleep(0.5)
    assert h["planner_enabled"] and h["query_mode"] == "graph", h
    yield base
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def _ask(page, base, question):
    page.goto(base + "/ui")
    page.click("nav button[data-v='ask']")     # /ui lands on the Overview
    page.wait_for_selector("#q")
    page.fill("#q", question)
    page.click("#go")
    page.wait_for_selector("#answer .trust .pill", timeout=60_000)   # the result, not the loading card
    return page.locator("#answer")


def _open_trace(page):
    if page.get_attribute("#advBtn", "aria-pressed") != "true":
        page.click("#advBtn")                   # trace links are an Advanced control
    page.click("#toTrace")
    page.wait_for_selector("#traceDetail .wf .sp", timeout=30_000)
    return [t.strip().lstrip("↳").strip() for t in page.locator("#traceDetail .wf .sp .n").all_inner_texts()]


def test_1_ask_answers_and_shows_its_trace(page, server):
    ans = _ask(page, server, ASK)
    assert "answered" in ans.inner_text().lower()
    page.click("#answer details.audit summary")
    assert "gold_hospital_profile" in page.locator("#answer details.audit pre").first.inner_text()
    names = _open_trace(page)
    for step in ("plan", "critic", "guard", "execute"):
        assert step in names, names


def test_2_a_guard_denial_names_the_rule_and_the_retries_are_traced(page, server):
    ans = _ask(page, server, DENY)
    text = ans.inner_text().lower()
    assert "refused" in text and "allowlist" in text
    assert "blocked at the boundary" in text     # every attempt was the guard's denial
    names = _open_trace(page)
    assert names.count("guard") >= 2 and "retry" in names, names


def _advanced_on(page):
    if page.get_attribute("#advBtn", "aria-pressed") != "true":
        page.click("#advBtn")


def _review(page, base, who):
    page.goto(base + "/ui")
    _advanced_on(page)                         # Review sits behind Advanced (item 9, D3a)
    page.click("nav button[data-v='review']")
    page.wait_for_selector("#reviewer")
    page.fill("#reviewer", who)
    page.dispatch_event("#reviewer", "change")


def _propose(page, question):
    page.fill("#propQ", question)
    page.click("#propGo")
    page.wait_for_selector(f"#queue .card:has-text(\"{question}\")", timeout=60_000)
    return page.locator(f"#queue .card:has-text(\"{question}\")")


def test_3_approval_commits_and_records_the_reviewer(page, server):
    _review(page, server, "e2e.reviewer")
    card = _propose(page, PROPOSE)
    assert "awaiting decision" in card.inner_text().lower()
    card.locator("button[data-act='approve']").click()
    # a selector wait, not wait_for_function: the page's CSP (item 33) forbids
    # evaluating JavaScript from a string, which is what wait_for_function does
    page.wait_for_selector(f"#queue .card:has-text(\"{PROPOSE}\")", state="detached",
                           timeout=60_000)
    # the decision is on the trace, under the reviewer's name
    traces = httpx.get(server + "/traces", headers={"X-API-Key": KEY}).json()["traces"]
    t = next(t for t in traces if t["name"] == "approval" and t["attrs"].get("question") == PROPOSE)
    full = httpx.get(f"{server}/traces/{t['trace_id']}", headers={"X-API-Key": KEY}).json()
    decisions = [s for s in full["spans"] if s["name"] == "approval.decision"]
    assert decisions and decisions[-1]["attrs"]["decision"] == "approve"
    assert full["attrs"].get("committed") is True


def test_3b_escalation_reassigns_and_stays_pending(page, server):
    _review(page, server, "e2e.nurse")
    card = _propose(page, ESCALATE)
    card.locator("button[data-act='esc-open']").click()
    card.locator("input[id^='escto-']").fill("dr.oncall")
    card.locator("button[data-act='escalate']").click()
    page.wait_for_selector(f"#queue .card:has-text(\"{ESCALATE}\"):has-text(\"dr.oncall\")",
                           timeout=30_000)
    pending = httpx.get(server + "/approvals", headers={"X-API-Key": KEY}).json()["pending"]
    item = next(i for i in pending if i.get("question") == ESCALATE)
    assert item["assigned_to"] == "dr.oncall"


def test_5_api_needs_the_key_and_traces_the_run(server):
    assert httpx.post(server + "/query", json={"question": ASK}).status_code == 401
    r = httpx.post(server + "/query", json={"question": ASK}, headers={"X-API-Key": KEY},
                   timeout=60).json()
    assert r["allowed"] and r["trace_id"]
    # a missing critic recording would only make the critic `unavailable` (it
    # fails open), so check its verdict came from a recording
    assert r["critic"] == "approve", r.get("critic")
    t = httpx.get(f"{server}/traces/{r['trace_id']}", headers={"X-API-Key": KEY}).json()
    assert [s["name"] for s in t["spans"]][:2] == ["plan", "critic"]


def test_6_every_recorded_critic_call_is_replayed(server):
    # The denied question is planned and reviewed on every attempt: each critic
    # verdict must be a recorded one, never `unavailable` from a ReplayMiss.
    r = httpx.post(server + "/query", json={"question": DENY}, headers={"X-API-Key": KEY},
                   timeout=60).json()
    verdicts = [s.get("verdict") for s in r.get("trajectory") or [] if s.get("step") == "critic"]
    assert verdicts and "unavailable" not in verdicts, verdicts


def test_7_answers_is_the_landing_view_with_three_tabs(page, server):
    page.goto(server + "/ui")
    assert page.locator("#v-overview").is_visible()
    visible = [b.inner_text().strip() for b in page.locator("nav button").all() if b.is_visible()]
    assert visible == ["Answers", "Ask", "About"], visible
    assert page.locator("#ovDefs dt").count() >= 6 and page.locator("#ovDefs").is_visible()
    assert "limits" in page.locator("#ovHow").inner_text().lower()
    # every fixed question answers with a number
    for i in range(page.locator("#ovQuestions button").count()):
        page.click(f"#ovQuestions button >> nth={i}")
        page.wait_for_selector("#ovAnswer .metric", timeout=30_000)
        text = page.locator("#ovAnswer").inner_text()
        assert any(ch.isdigit() for ch in text) and "Unavailable" not in text
    # the raw controls and the other views are hidden until Advanced is switched on
    assert not page.locator("nav button[data-v='review']").is_visible()
    page.click("nav button[data-v='ask']")
    assert not page.locator("#segBackend").is_visible()
    page.click("#advBtn")
    assert page.locator("#segBackend").is_visible()
    assert page.locator("nav button[data-v='traces']").is_visible()
    page.click("#advBtn")
    assert not page.locator("#segBackend").is_visible()


def test_8_a_first_time_visitor_with_no_key_gets_an_answer_in_one_click(page, server):
    """The deployed case (item 9's done-when): no key saved and none prefilled, as on
    Render. The page must reach a useful answer in at most two clicks, show the
    definitions on the first screen, have the cold-start note, and make no call
    that fails (the nightly live check fails on any console error)."""
    def no_prefill(route):
        resp = route.fetch()
        cfg = resp.json()
        cfg["prefill_key"], cfg["auth_required"] = None, True
        route.fulfill(response=resp, json=cfg)

    failed, errors = [], []
    page.on("response", lambda r: r.status >= 400 and failed.append((r.status, r.url)))
    page.on("console", lambda m: m.type == "error" and errors.append(m.text))
    page.route("**/ui-config", no_prefill)
    page.goto(server + "/ui")
    page.evaluate("localStorage.clear()")
    page.reload(wait_until="networkidle")
    assert page.locator("#ovDefs").is_visible()            # definitions on the first screen
    assert page.locator("#coldNote").count() == 1          # the cold-start note exists
    page.click("#ovQuestions button >> nth=0")             # click 1
    page.wait_for_selector("#ovAnswer .metric", timeout=30_000)
    text = page.locator("#ovAnswer").inner_text()
    assert any(ch.isdigit() for ch in text) and "Unavailable" not in text
    assert failed == [] and errors == [], (failed, errors)
