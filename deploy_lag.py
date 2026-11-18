"""Alert when the live deploy falls behind main (owner, 2026-11-18).

Render auto-deploys main, but a failed build leaves the old deploy serving and
the nightly live check still passes (it loads the old code). This compares the
commit the deploy reports in `/v1/health` (`git_commit`, from
RENDER_GIT_COMMIT) with main's head:

  same commit                    -> healthy; closes an open alert
  main moved less than the grace -> pending (a deploy takes a few minutes)
  otherwise                      -> one issue, label `deploy-lag`,
                                    "deploy behind main"; a repeat comments

The grace is DEPLOY_LAG_GRACE_MIN (default 60) minutes after main's newest
commit. An unreachable service or an unknown commit files nothing (the live
check reports reachability). Runs in live-check.yml after the check:

    python deploy_lag.py [--dry-run]                        # needs GH_TOKEN
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import urllib.request
from typing import Any

SERVICE_URL = os.environ.get("SERVICE_URL", "https://governed-clinical-agent.onrender.com")
REPO = os.environ.get("GITHUB_REPOSITORY", "erjonb19/governed-clinical-agent")
TITLE = "deploy behind main"
LABEL = "deploy-lag"
HEALTHY, PENDING, LAGGING, UNKNOWN = "healthy", "pending", "lagging", "unknown"
_SHA = re.compile(r"[0-9a-f]{40}")


def classify(live: str, main: str, main_age_min: float, grace_min: float) -> str:
    if not _SHA.fullmatch(live or ""):
        return UNKNOWN
    if live == main:
        return HEALTHY
    return PENDING if main_age_min < grace_min else LAGGING


def actions(
    verdict: str, open_issue: int | None, detail: str, run_url: str = ""
) -> list[dict[str, Any]]:
    link = f"\n\nRun: {run_url}" if run_url else ""
    if verdict == LAGGING:
        body = f"{detail}{link}"
        return [
            {"action": "comment", "number": open_issue, "body": body}
            if open_issue
            else {"action": "open", "title": TITLE, "body": body}
        ]
    if verdict == HEALTHY and open_issue:
        return [
            {"action": "close", "number": open_issue, "body": f"The deploy is on main again.{link}"}
        ]
    return []


def _gh(*args: str) -> str:
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def live_commit(timeout: float = 180.0) -> str:
    if not SERVICE_URL.startswith("https://"):
        raise ValueError("SERVICE_URL must be https")
    with urllib.request.urlopen(SERVICE_URL.rstrip("/") + "/v1/health", timeout=timeout) as r:  # noqa: S310
        return str(json.load(r).get("git_commit") or "")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    try:
        live = live_commit()
    except Exception as e:  # noqa: BLE001 -- reachability is live_check.py's job
        print(f"::warning::could not read /v1/health ({type(e).__name__}); no deploy-lag check")
        return 0
    head = json.loads(_gh("api", f"repos/{REPO}/commits/main"))
    main_sha = head["sha"]
    when = dt.datetime.fromisoformat(head["commit"]["committer"]["date"].replace("Z", "+00:00"))
    age_min = (dt.datetime.now(dt.timezone.utc) - when).total_seconds() / 60
    grace = float(os.environ.get("DEPLOY_LAG_GRACE_MIN", "60") or 60)
    verdict = classify(live, main_sha, age_min, grace)
    if verdict == UNKNOWN:
        print(
            f"::warning::/v1/health reports git_commit {live[:40]!r}, not a commit: "
            "the deploy cannot be compared with main (is RENDER_GIT_COMMIT set?)"
        )
    detail = ""
    if verdict == LAGGING:
        try:
            cmp = json.loads(_gh("api", f"repos/{REPO}/compare/{live}...{main_sha}"))
        except Exception:  # noqa: BLE001 -- GitHub may not know the live commit; still alert
            cmp = {"status": "unknown to GitHub", "ahead_by": "?"}
        detail = (
            f"The deploy serves `{live[:7]}`; main is `{main_sha[:7]}` "
            f"({cmp.get('status')}, {cmp.get('ahead_by')} commit(s) ahead of the deploy), "
            f"and main's newest commit is {age_min / 60:.1f} h old. Check the newest deploy's "
            "build log in the Render dashboard (Events)."
        )
    run_url = ""
    if os.environ.get("GITHUB_RUN_ID"):
        run_url = (
            f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/"
            f"{REPO}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
        )
    listed = (
        []
        if args.dry_run
        else json.loads(
            _gh("issue", "list", "--state", "open", "--limit", "1000", "--json", "number,title")
        )
    )
    open_issue = next((i["number"] for i in listed if i.get("title") == TITLE), None)
    todo = actions(verdict, open_issue, detail, run_url)
    print(f"live {live[:7] or '?'} vs main {main_sha[:7]} ({age_min:.0f} min old): {verdict}")
    for a in todo:
        print(f"  {a['action']}: {a.get('title') or '#' + str(a.get('number'))}")
        if args.dry_run:
            continue
        if a["action"] == "open":
            _gh(
                "label",
                "create",
                LABEL,
                "--force",
                "--color",
                "D93F0B",
                "--description",
                "Opened by deploy_lag.py: the deploy is behind main",
            )
            _gh("issue", "create", "--title", a["title"], "--body", a["body"], "--label", LABEL)
        elif a["action"] == "comment":
            _gh("issue", "comment", str(a["number"]), "--body", a["body"])
        else:
            _gh("issue", "close", str(a["number"]), "--comment", a["body"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
