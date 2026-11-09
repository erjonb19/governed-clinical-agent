"""Alerts for the nightly eval as GitHub issues (backlog item 3).

Alerts for the nightly eval (backlog item 3): a GitHub issue, never mixed
between two kinds (owner's decision 2026-11-06):

  label `eval-regression`  "nightly eval regression: <dataset>"
      the score really dropped -- accuracy over completed runs under the gate
      (80%, eval_harness.THRESHOLD) -- or the harness broke before it could
      score (no report);
  label `provider-outage`  "nightly provider outage: <dataset>"
      inconclusive after the one retry: more than 20% of planned runs never
      reached the provider, a case was answered fewer than 2 times, or an
      attempt ran past its time limit. Says nothing about accuracy.

One issue per kind per dataset. A repeat comments on the open one; the next
healthy run (gate passed) closes both. An outage leaves an open regression
issue alone (an outage proves nothing either way); a run that scores --
pass or fail -- closes an open outage issue, because the provider answered.

Why these thresholds: they are the gate's own bars, so an alert means exactly
"the nightly went red", for a reason the issue names. A tighter accuracy bar
would page on model noise: one FHIR case at 1 of 3 runs is ~2.4 points, and
main's own nightly has had one such case.

The verdict comes from the gate's exit code (scripts/eval_gate.sh: 0 pass,
1 below the gate, 2 inconclusive twice), and the numbers from the newest
report in eval_runs/. Run in CI:

    python nightly_alerts.py --dataset hospital --gate-exit "$RC"   # needs GH_TOKEN
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
from typing import Any

Report = dict[str, Any]  # one eval_harness report (eval_runs/*.json)

REGRESSION, OUTAGE, HEALTHY = "regression", "outage", "healthy"
LABELS = {REGRESSION: "eval-regression", OUTAGE: "provider-outage"}
TITLES = {REGRESSION: "nightly eval regression: {ds}", OUTAGE: "nightly provider outage: {ds}"}
COLORS = {REGRESSION: "B60205", OUTAGE: "FBCA04"}


def classify(gate_exit: int, report: Report | None) -> str:
    """The night's verdict for one dataset."""
    if gate_exit == 0:
        return HEALTHY
    if gate_exit == 2:
        return OUTAGE
    return REGRESSION  # 1 (below the gate), or the job broke: red, investigate


def _numbers(report: Report | None) -> str:
    if not report:
        return (
            "No report was written during this job: the harness stopped before scoring, "
            "or every attempt ran past its time limit (see the run log)."
        )
    total = (report.get("completed_runs") or 0) + (report.get("provider_error_runs") or 0)
    lines = [
        f"- status: `{report.get('status')}`",
        f"- accuracy over completed runs: {report.get('completed_accuracy', 0):.1%} "
        f"(gate {report.get('threshold', 0.8):.0%})",
        f"- completed runs: {report.get('completed_runs')} of {total}; "
        f"provider errors: {report.get('provider_error_runs')}",
        f"- provider: {report.get('provider')} ({report.get('model')})",
    ]
    failed = [c for c in report.get("cases", []) if not c.get("passed")]
    if failed:
        lines.append(
            "- cases below a majority: "
            + ", ".join(f"`{c['id']}` {c.get('correct_runs')}/{c.get('runs')}" for c in failed[:15])
        )
    unanswered = report.get("unanswered_cases") or []
    if unanswered:
        lines.append("- cases hardly answered: " + ", ".join(f"`{c}`" for c in unanswered[:15]))
    return "\n".join(lines)


def actions(
    dataset: str,
    verdict: str,
    report: Report | None,
    open_issues: dict[str, int],
    run_url: str = "",
) -> list[dict[str, Any]]:
    """What to do on GitHub. open_issues is {title: number} of open issues."""
    link = f"\n\nRun: {run_url}" if run_url else ""
    when = (report or {}).get("timestamp", "")
    title = {k: t.format(ds=dataset) for k, t in TITLES.items()}
    out: list[dict[str, Any]] = []
    if verdict in (REGRESSION, OUTAGE):
        what = (
            "Accuracy fell below the gate, or the harness broke."
            if verdict == REGRESSION
            else "Inconclusive after one retry: the provider was unavailable or cases went "
            "unanswered. **Not an accuracy regression.**"
        )
        body = f"**{dataset}** nightly eval {when}: {what}\n\n{_numbers(report)}{link}"
        n = open_issues.get(title[verdict])
        out.append(
            {"action": "comment", "number": n, "body": body}
            if n
            else {"action": "open", "title": title[verdict], "label": LABELS[verdict], "body": body}
        )
    # the provider answered (pass or fail): an open outage issue is over. Only
    # on a report that really scored -- after an outage, a crashed retry leaves
    # the outage report as the newest one
    scored = report is not None and report.get("status") not in (
        "provider_unavailable",
        "incomplete",
    )
    if verdict in (REGRESSION, HEALTHY) and scored and open_issues.get(title[OUTAGE]):
        out.append(
            {
                "action": "close",
                "number": open_issues[title[OUTAGE]],
                "body": f"The provider answered again ({when}).{link}",
            }
        )
    if verdict == HEALTHY and open_issues.get(title[REGRESSION]):
        out.append(
            {
                "action": "close",
                "number": open_issues[title[REGRESSION]],
                "body": f"Healthy again ({when}):\n\n{_numbers(report)}{link}",
            }
        )
    return out


def latest_report(dataset: str, since: float = 0.0, out_dir: str = "eval_runs") -> Report | None:
    """The newest report for this dataset written after `since` (epoch seconds)."""
    paths = [
        p
        for p in glob.glob(os.path.join(out_dir, f"eval_*_{dataset}_*.json"))
        if os.path.getmtime(p) >= since
    ]
    if not paths:
        return None
    with open(max(paths, key=os.path.getmtime), encoding="utf-8") as f:
        report: Report = json.load(f)
    return report


def open_issues_by_title(listed: list[dict[str, Any]]) -> dict[str, int]:
    """{title: number} of our open issues, matched by exact title (oldest wins),
    so a label removed by hand never causes a duplicate."""
    ours = {t.format(ds="") for t in TITLES.values()}
    out: dict[str, int] = {}
    for i in listed:
        t = i.get("title") or ""
        if any(t.startswith(p) for p in ours) and (t not in out or i["number"] < out[t]):
            out[t] = i["number"]
    return out


def _gh(*args: str) -> str:
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--gate-exit", type=int, required=True)
    ap.add_argument(
        "--since", type=float, default=0.0, help="only reports written after this epoch time count"
    )
    ap.add_argument("--dry-run", action="store_true", help="print the actions, call nothing")
    args = ap.parse_args()

    report = latest_report(args.dataset, args.since)
    verdict = classify(args.gate_exit, report)
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
    todo = actions(args.dataset, verdict, report, open_issues_by_title(listed), run_url)
    print(f"{args.dataset}: gate exit {args.gate_exit} -> {verdict}; {len(todo)} action(s)")
    for a in todo:
        print(f"  {a['action']}: {a.get('title') or '#' + str(a.get('number'))}")
        if args.dry_run:
            continue
        if a["action"] == "open":
            kind = REGRESSION if a["label"] == LABELS[REGRESSION] else OUTAGE
            _gh(
                "label",
                "create",
                a["label"],
                "--force",
                "--color",
                COLORS[kind],
                "--description",
                "Opened by the nightly eval (nightly_alerts.py)",
            )
            _gh(
                "issue", "create", "--title", a["title"], "--body", a["body"], "--label", a["label"]
            )
        elif a["action"] == "comment":
            _gh("issue", "comment", str(a["number"]), "--body", a["body"])
        elif a["action"] == "close":
            _gh("issue", "close", str(a["number"]), "--comment", a["body"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
