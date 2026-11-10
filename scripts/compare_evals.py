"""Compare a branch's eval report with main's (backlog item 11).

The manual FHIR eval used to fail unless every case passed, a bar model noise
alone can fail. Now it scores the branch AND main in one workflow and passes
only if the branch's accuracy over completed runs is at least main's. Either
run being inconclusive (provider unavailable, or a case left unanswered) is
not a pass.

    python scripts/compare_evals.py eval_runs/branch eval_runs/main

Exit 0 pass, 1 the branch scored below main, 2 inconclusive.
"""

from __future__ import annotations

import glob
import json
import os
import sys
from typing import Any

INCONCLUSIVE = ("provider_unavailable", "incomplete")


def newest_report(path: str) -> dict[str, Any] | None:
    """A report file, or the newest eval_*.json in a directory."""
    if os.path.isdir(path):
        found = glob.glob(os.path.join(path, "eval_*.json"))
        if not found:
            return None
        path = max(found, key=os.path.getmtime)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def compare(branch: dict[str, Any] | None, main: dict[str, Any] | None) -> tuple[int, list[str]]:
    lines: list[str] = []
    for name, r in (("branch", branch), ("main", main)):
        if r is None:
            return 2, [f"{name}: no report (the eval did not finish)"]
    b_acc, m_acc = branch["completed_accuracy"], main["completed_accuracy"]
    lines.append("| case | branch | main |")
    lines.append("|---|---|---|")
    m_cases = {c["id"]: c for c in main.get("cases", [])}
    for c in branch.get("cases", []):
        m = m_cases.get(c["id"])
        mark = "" if m is None or c["passed"] == m["passed"] else (" ▲" if c["passed"] else " ▼")
        lines.append(
            f"| {c['id']} | {c['correct_runs']}/{c['runs']}{mark} | "
            + (f"{m['correct_runs']}/{m['runs']}" if m else "-")
            + " |"
        )
    lines.append(
        f"| **accuracy over completed runs** | **{b_acc:.1%}** "
        f"({branch['completed_runs']} runs, {branch['status']}) | **{m_acc:.1%}** "
        f"({main['completed_runs']} runs, {main['status']}) |"
    )
    if branch["status"] in INCONCLUSIVE or main["status"] in INCONCLUSIVE:
        lines.append(
            "INCONCLUSIVE: a run did not complete enough of its cases "
            "(provider unavailable or a case unanswered). Not a pass."
        )
        return 2, lines
    if b_acc + 1e-9 < m_acc:
        lines.append(f"FAIL: the branch ({b_acc:.1%}) scored below main ({m_acc:.1%}).")
        return 1, lines
    lines.append(f"PASS: the branch ({b_acc:.1%}) is at least main ({m_acc:.1%}).")
    return 0, lines


def main_cli(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2
    code, lines = compare(newest_report(argv[1]), newest_report(argv[2]))
    print("\n".join(lines))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("## FHIR eval: branch vs main\n\n" + "\n".join(lines) + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main_cli(sys.argv))
