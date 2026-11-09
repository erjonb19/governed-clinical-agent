"""Service level objectives, checked weekly, with a GitHub issue on a miss (backlog item 7).

What is promised, and what is not:

* STACK LATENCY -- this repository's own code: the API, the graph, the guard,
  the PHI checks, DuckDB and tracing, measured by `scripts/load_test.py` on
  RECORDED model answers (no provider involved). These are the targets below,
  checked weekly by `load-test.yml` on a GitHub runner.
* AVAILABILITY of the deploy -- `/health` and `/ui` load with no console
  error. Checked nightly by `live-check.yml` (its own `live-check` issue).
* MODEL LATENCY is the provider's (Gemini's free tier), so there is no target
  for it. `/metrics` reports it per call: p50, p95 and p99.

Where the numbers come from (docs/benchmarks.md, item 20): in the session
sandbox (4 CPUs), 5 concurrent users measured p95 145 ms and p99 154 ms before
item 20's tracer fix, and 108 ms and 126 ms after it. A GitHub runner has 2 to
4 vCPUs and noisier neighbours, so the targets leave about 2.5x headroom over
the worse of the two. A miss therefore means a real slowdown, not noise.
Tighten the targets once a few weekly runner results exist.

    python slo.py load.json                 # print the verdict; exit 1 on a miss
    python slo.py load.json --alert         # also open/comment/close the issue (needs GH_TOKEN)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from typing import Any

# (concurrent users, metric, limit). Every level the load test ran is also
# checked for errors: any failed request is a miss.
TARGETS: list[tuple[int, str, float]] = [
    (1, "p95_ms", 150.0),
    (5, "p95_ms", 400.0),
    (5, "p99_ms", 600.0),
]
TITLE = "SLO missed: stack latency"
LABEL = "slo-miss"


def check(result: dict[str, Any]) -> list[str]:
    """The misses in one load-test result (scripts/load_test.py --json)."""
    levels = {r["users"]: r for r in result.get("results", [])}
    misses = []
    for users, metric, limit in TARGETS:
        r = levels.get(users)
        if r is None:
            misses.append(f"{users} users: not measured")
            continue
        v = r.get(metric)
        if v is None or v > limit:
            misses.append(f"{users} users: {metric} {v} ms > {limit:g} ms")
    for users, r in sorted(levels.items()):
        if r.get("ok") != r.get("requests") or r.get("errors"):
            misses.append(
                f"{users} users: {r.get('requests', 0) - r.get('ok', 0)} failed "
                f"requests {r.get('errors')}"
            )
    return misses


def table(result: dict[str, Any]) -> str:
    rows = ["| users | p50 | p95 | p99 | ok | throughput |", "|---|---|---|---|---|---|"]
    for r in result.get("results", []):
        rows.append(
            f"| {r['users']} | {r['p50_ms']} ms | {r['p95_ms']} ms | {r['p99_ms']} ms | "
            f"{r['ok']}/{r['requests']} | {r['throughput_rps']} req/s |"
        )
    return "\n".join(rows)


def actions(
    misses: list[str], result: dict[str, Any], open_number: int | None, run_url: str = ""
) -> list[dict[str, Any]]:
    """One issue for all misses: open it, comment on it while it stays open,
    close it on the first run that meets every target."""
    link = f"\n\nRun: {run_url}" if run_url else ""
    machine = result.get("machine", "")
    if misses:
        body = (
            "Stack latency missed its SLO (`slo.py`, recorded answers, no model):\n\n"
            + "\n".join(f"- {m}" for m in misses)
            + f"\n\n{table(result)}\n\nMachine: {machine}{link}"
        )
        if open_number:
            return [{"action": "comment", "number": open_number, "body": body}]
        return [{"action": "open", "title": TITLE, "label": LABEL, "body": body}]
    if open_number:
        return [
            {
                "action": "close",
                "number": open_number,
                "body": f"Every SLO target met again.\n\n{table(result)}{link}",
            }
        ]
    return []


def _gh(*args: str) -> str:
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("result", help="scripts/load_test.py --json output")
    ap.add_argument("--alert", action="store_true", help="open/comment/close the GitHub issue")
    args = ap.parse_args()
    with open(args.result, encoding="utf-8") as f:
        result = json.load(f)
    misses = check(result)
    print(table(result))
    print("SLO: " + ("MISSED\n  " + "\n  ".join(misses) if misses else "met"))
    if args.alert:
        listed = json.loads(
            _gh("issue", "list", "--state", "open", "--limit", "1000", "--json", "number,title")
        )
        mine = sorted(i["number"] for i in listed if i.get("title") == TITLE)
        run_url = ""
        if os.environ.get("GITHUB_RUN_ID"):
            run_url = (
                f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/"
                f"{os.environ.get('GITHUB_REPOSITORY')}/actions/runs/"
                f"{os.environ['GITHUB_RUN_ID']}"
            )
        for a in actions(misses, result, mine[0] if mine else None, run_url):
            print(f"  {a['action']}: {a.get('title') or '#' + str(a.get('number'))}")
            if a["action"] == "open":
                _gh(
                    "label",
                    "create",
                    LABEL,
                    "--force",
                    "--color",
                    "D93F0B",
                    "--description",
                    "Opened by the weekly SLO check (slo.py)",
                )
                _gh(
                    "issue",
                    "create",
                    "--title",
                    a["title"],
                    "--body",
                    a["body"],
                    "--label",
                    a["label"],
                )
            elif a["action"] == "comment":
                _gh("issue", "comment", str(a["number"]), "--body", a["body"])
            else:
                _gh("issue", "close", str(a["number"]), "--comment", a["body"])
    return 1 if misses else 0


if __name__ == "__main__":
    sys.exit(main())
