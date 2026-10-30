"""
live_check.py
=============
Nightly check that the deployed service is actually up (PLAN v2 7d).

Loads `<url>/health` over HTTP and `<url>/ui` in headless Chromium, with no API
key, so it calls no LLM and costs nothing. One attempt passes when:

    /health    HTTP 200 and {"status": "ok"}
    /ui        HTTP 200, the title is the app's, the nav and main shell render
    console    no console errors and no uncaught page errors during the load

Render's free tier sleeps, and the first request after a nap can take a minute
or more. So a failed attempt is retried: ATTEMPTS attempts, each allowed
TIMEOUT_S, PAUSE_S apart. Only when every attempt fails is the check a failure.

With --file-issue (CI), the outcome is reflected in ONE GitHub issue, labelled
`live-check`: a failure opens it, or comments on it if it is already open (never
a duplicate); the next pass comments "recovered" and closes it.

    python live_check.py                                  # check the deploy
    python live_check.py --url http://127.0.0.1:8000      # check a local server
    python live_check.py --file-issue --screenshot-dir shots   # CI

Exit 0 pass, 1 fail.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from typing import Callable, Optional

DEFAULT_URL = "https://governed-clinical-agent.onrender.com"
ATTEMPTS = 3
TIMEOUT_S = 90
PAUSE_S = 30
TITLE_MARK = "Clinical Intelligence"

ISSUE_LABEL = "live-check"
ISSUE_TITLE = "live check: the deployed service is down"


def problems(health_status: Optional[int], health_body: Optional[dict],
             ui_status: Optional[int], title: str, shell_ok: bool,
             console_errors: list[str]) -> list[str]:
    """Everything wrong with one attempt; an empty list is a pass."""
    out = []
    if health_status != 200:
        out.append(f"/health returned {health_status}")
    elif (health_body or {}).get("status") != "ok":
        out.append(f"/health status is {(health_body or {}).get('status')!r}, not 'ok'")
    if ui_status != 200:
        out.append(f"/ui returned {ui_status}")
    else:
        if TITLE_MARK not in (title or ""):
            out.append(f"/ui title is {title!r}")
        if not shell_ok:
            out.append("/ui app shell (nav, main) did not render")
    for e in console_errors:
        out.append(f"console error: {e[:200]}")
    return out


def check_once(url: str, timeout_s: int, screenshot: Optional[str] = None) -> list[str]:
    """One attempt against a live server. Returns its problems."""
    import httpx
    from playwright.sync_api import sync_playwright

    health_status, health_body = None, None
    try:
        r = httpx.get(f"{url}/health", timeout=timeout_s)
        health_status = r.status_code
        health_body = r.json() if r.status_code == 200 else None
    except Exception as e:
        return [f"/health unreachable: {type(e).__name__}: {e}"[:300]]

    console: list[str] = []
    ui_status, title, shell_ok = None, "", False
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.on("console", lambda m: console.append(m.text) if m.type == "error" else None)
            page.on("pageerror", lambda e: console.append(f"uncaught: {e}"))
            resp = page.goto(f"{url}/ui", timeout=timeout_s * 1000, wait_until="networkidle")
            ui_status = resp.status if resp else None
            title = page.title()
            shell_ok = page.locator("#nav").count() == 1 and page.locator("main").count() == 1
            if screenshot:
                page.screenshot(path=screenshot, full_page=True)
            browser.close()
    except Exception as e:
        return [f"/ui did not load: {type(e).__name__}: {e}"[:300]]
    return problems(health_status, health_body, ui_status, title, shell_ok, console)


def check(url: str, attempts: int = ATTEMPTS, timeout_s: int = TIMEOUT_S,
          pause_s: int = PAUSE_S, screenshot_dir: Optional[str] = None,
          once: Callable[..., list[str]] = check_once,
          sleep: Callable[[float], None] = time.sleep) -> dict:
    """Retry until an attempt passes or `attempts` have failed."""
    tries = []
    for n in range(1, attempts + 1):
        shot = os.path.join(screenshot_dir, f"attempt-{n}.png") if screenshot_dir else None
        found = once(url, timeout_s, shot)
        tries.append({"attempt": n, "problems": found})
        if not found:
            return {"ok": True, "url": url, "attempts": tries}
        if n < attempts:
            sleep(pause_s)
    return {"ok": False, "url": url, "attempts": tries}


def issue_action(result: dict, open_issue: Optional[int], run_url: str = "") -> Optional[dict]:
    """What to do on GitHub. One issue at most: comment rather than duplicate."""
    link = f"\n\nRun: {run_url}" if run_url else ""
    if not result["ok"]:
        lines = [f"**{result['url']}** failed all {len(result['attempts'])} attempts.", ""]
        for t in result["attempts"]:
            lines.append(f"- attempt {t['attempt']}: " + "; ".join(t["problems"]))
        body = "\n".join(lines) + "\n\nScreenshots are attached to the run as artifacts." + link
        if open_issue:
            return {"action": "comment", "number": open_issue, "body": body}
        return {"action": "open", "title": ISSUE_TITLE, "body": body}
    if open_issue:
        n = len(result["attempts"])
        return {"action": "close", "number": open_issue,
                "body": f"Recovered: {result['url']} passed on attempt {n}.{link}"}
    return None


def _gh(*args: str) -> str:
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def file_issue(result: dict) -> None:
    """Apply issue_action() with the gh CLI. Needs GH_TOKEN with issues: write."""
    _gh("label", "create", ISSUE_LABEL, "--force", "--color", "B60205",
        "--description", "Opened by the nightly live check of the deployed service")
    listed = json.loads(_gh("issue", "list", "--label", ISSUE_LABEL, "--state", "open",
                            "--limit", "20", "--json", "number"))
    open_issue = min((i["number"] for i in listed), default=None)
    run_url = ""
    if os.environ.get("GITHUB_RUN_ID"):
        run_url = (f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/"
                   f"{os.environ.get('GITHUB_REPOSITORY')}/actions/runs/{os.environ['GITHUB_RUN_ID']}")
    a = issue_action(result, open_issue, run_url)
    if a is None:
        return
    if a["action"] == "open":
        _gh("issue", "create", "--title", a["title"], "--body", a["body"], "--label", ISSUE_LABEL)
    elif a["action"] == "comment":
        _gh("issue", "comment", str(a["number"]), "--body", a["body"])
    elif a["action"] == "close":
        _gh("issue", "close", str(a["number"]), "--comment", a["body"])
    print(f"issue {a['action']}: {a.get('title') or '#' + str(a.get('number'))}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--url", default=os.environ.get("LIVE_CHECK_URL", DEFAULT_URL))
    ap.add_argument("--attempts", type=int, default=ATTEMPTS)
    ap.add_argument("--timeout", type=int, default=TIMEOUT_S)
    ap.add_argument("--pause", type=int, default=PAUSE_S)
    ap.add_argument("--screenshot-dir")
    ap.add_argument("--file-issue", action="store_true",
                    help="open/comment/close the live-check issue (CI)")
    args = ap.parse_args()
    url = args.url.rstrip("/")
    if args.screenshot_dir:
        os.makedirs(args.screenshot_dir, exist_ok=True)

    result = check(url, args.attempts, args.timeout, args.pause, args.screenshot_dir)
    for t in result["attempts"]:
        print(f"attempt {t['attempt']}: " + ("ok" if not t["problems"] else "; ".join(t["problems"])))
    print("PASS" if result["ok"] else "FAIL")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(f"### Live check of {url}: {'PASS' if result['ok'] else 'FAIL'}\n\n")
            for t in result["attempts"]:
                f.write(f"- attempt {t['attempt']}: "
                        + ("ok" if not t["problems"] else "; ".join(t["problems"])) + "\n")
    if args.file_issue:
        file_issue(result)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
