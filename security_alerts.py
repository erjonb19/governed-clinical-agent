"""Nightly security alerts as GitHub issues (backlog item 59).

Reads the deployed service's `/v1/metrics` with a key (the repo secret
`ALERT_API_KEY`) and opens ONE issue per kind, labelled `security-alert`:

  "security alert: refused API keys"   a client is over the failed-key limit
      right now, or refused keys since the last restart reach
      SECURITY_ALERT_AUTH_TOTAL (default 50);
  "security alert: model spend"        today's model runs or estimated spend
      reach SECURITY_ALERT_BUDGET_FRACTION (default 0.8) of the daily caps
      (llm_budget.py).

A repeat comments on the open issue; the next quiet night closes it. The
counters live in the service's memory, so they cover the time since its last
restart (Render free sleeps when idle): a quiet night after a restart is not
proof that nothing happened, and "a client over the limit" means within the
current 300 s window. It runs at 23:30 UTC, late in the budget day (the caps
reset at UTC midnight), and allows 180 s for a sleeping service to wake. Without the secret, or when the service cannot
be reached (live_check.py reports that), the job warns and files nothing.
Nothing in an issue names a client address or a key.

    python security_alerts.py [--dry-run]        # needs ALERT_API_KEY, GH_TOKEN
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.request
from typing import Any

SERVICE_URL = os.environ.get("SERVICE_URL", "https://governed-clinical-agent.onrender.com")
AUTH, SPEND = "auth", "spend"
TITLES = {AUTH: "security alert: refused API keys", SPEND: "security alert: model spend"}
LABEL = "security-alert"


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def classify(metrics: dict[str, Any]) -> dict[str, str]:
    """{kind: one-line reason} for every alert that fires."""
    out: dict[str, str] = {}
    af = metrics.get("auth_failures") or {}
    total_limit = _float_env("SECURITY_ALERT_AUTH_TOTAL", 50)
    if (af.get("clients_over_limit") or 0) > 0:
        out[AUTH] = (
            f"{af['clients_over_limit']} client(s) over the failed-key limit "
            f"({af.get('per_window')} in {af.get('window_sec')} s) right now"
        )
    elif (af.get("total") or 0) >= total_limit:
        out[AUTH] = f"{af['total']} refused keys since the last restart (alert at {total_limit:g})"
    b = metrics.get("llm_budget") or {}
    frac = _float_env("SECURITY_ALERT_BUDGET_FRACTION", 0.8)
    for used, cap, what in (
        ("runs", "max_runs", "model runs"),
        ("est_usd", "max_usd", "estimated USD"),
    ):
        if b.get(cap) and (b.get(used) or 0) >= frac * b[cap]:
            out[SPEND] = f"{b.get(used)} of {b[cap]} {what} used today ({b.get('day')})"
            break
    return out


def actions(
    firing: dict[str, str], open_issues: dict[str, int], run_url: str = ""
) -> list[dict[str, Any]]:
    link = f"\n\nRun: {run_url}" if run_url else ""
    out: list[dict[str, Any]] = []
    for kind, title in TITLES.items():
        n = open_issues.get(title)
        if kind in firing:
            body = f"{firing[kind]}.{link}"
            out.append(
                {"action": "comment", "number": n, "body": body}
                if n
                else {"action": "open", "title": title, "body": body}
            )
        elif n:
            out.append({"action": "close", "number": n, "body": f"Quiet again.{link}"})
    return out


def fetch_metrics(key: str, timeout: float = 180.0) -> dict[str, Any]:
    if not SERVICE_URL.startswith("https://"):
        raise ValueError("SERVICE_URL must be https: the key travels in a header")
    req = urllib.request.Request(
        SERVICE_URL.rstrip("/") + "/v1/metrics", headers={"X-API-Key": key}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 -- fixed https URL
        data: dict[str, Any] = json.load(r)
    return data


def _gh(*args: str) -> str:
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    key = os.environ.get("ALERT_API_KEY", "")
    if not key:
        print("::warning::ALERT_API_KEY is not set; no security alert check (docs/OWNER_STEPS.md)")
        return 0
    try:
        metrics = fetch_metrics(key)
    except Exception as e:  # noqa: BLE001 -- reachability is live_check.py's job
        print(f"::warning::could not read /v1/metrics ({type(e).__name__}); no alert filed")
        return 0
    firing = classify(metrics)
    run_url = ""
    if os.environ.get("GITHUB_RUN_ID"):
        run_url = (
            f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/"
            f"{os.environ.get('GITHUB_REPOSITORY')}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
        )
    listed = (
        []
        if args.dry_run
        else json.loads(
            _gh("issue", "list", "--state", "open", "--limit", "1000", "--json", "number,title")
        )
    )
    open_issues: dict[str, int] = {}
    for i in listed:
        if i.get("title") in TITLES.values():
            open_issues.setdefault(i["title"], i["number"])
    todo = actions(firing, open_issues, run_url)
    print(f"firing: {sorted(firing) or 'none'}; {len(todo)} action(s)")
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
                "B60205",
                "--description",
                "Opened by security_alerts.py (backlog item 59)",
            )
            _gh("issue", "create", "--title", a["title"], "--body", a["body"], "--label", LABEL)
        elif a["action"] == "comment":
            _gh("issue", "comment", str(a["number"]), "--body", a["body"])
        else:
            _gh("issue", "close", str(a["number"]), "--comment", a["body"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
