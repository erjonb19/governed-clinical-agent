"""The API and the UI end to end, in a real browser, on recorded LLM responses (PLAN v2 7a).

A real `uvicorn app:app` serves the committed hospital Gold with
PLANNER_PROVIDER=replay: every plan and critic call is answered from
tests/e2e/recordings/llm.json, a miss fails loudly, and nothing reaches a
provider. Chromium (pytest-playwright) drives the UI.

    1 ask             question -> answer, the SQL, and its trace
                      (plan -> critic -> guard -> execute)
    2 guard denial    a question whose recorded SQL reads a bronze table: the
                      refusal names the rule, and the retries are in the trace
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
           "PLANNER_PROVIDER": "replay", "API_KEY": KEY, "QUERY_MODE": "graph",
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
    page.wait_for_selector("#q")
    page.fill("#q", question)
    page.click("#go")
    page.wait_for_selector("#answer .trust .pill", timeout=60_000)   # the result, not the loading card
    return page.locator("#answer")


def _open_trace(page):
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


def _review(page, base, who):
    page.goto(base + "/ui")
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
    page.wait_for_function(
        f"!document.querySelector('#queue') || ![...document.querySelectorAll('#queue .card')]"
        f".some(c => c.innerText.includes({PROPOSE!r}))", timeout=60_000)
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
    t = httpx.get(f"{server}/traces/{r['trace_id']}", headers={"X-API-Key": KEY}).json()
    assert [s["name"] for s in t["spans"]][:2] == ["plan", "critic"]
