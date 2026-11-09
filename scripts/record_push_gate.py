"""Write the push gate's recorded model answers (backlog item 13).

The push gate (`eval-on-push.yml`) replays committed answers instead of calling a
model: zero tokens, seconds, and none of Gemini's daily quota, which the nightly
needs. It still runs the real harness, guard, data and scoring, so it catches a
broken guard, schema note, harness or Gold. It does not measure a model: the
nightly and the manual FHIR eval stay live on Gemini for that.

Each recording is keyed on the exact request the harness makes (replay.py:
role, a hash of the system prompt, the question). Change a prompt, a schema note
or a question without re-recording and the push gate fails with REPLAY MISS, and
so does `tests/unit/test_push_gate_recordings.py` -- re-run this script in the
same PR.

Two modes:

    python scripts/record_push_gate.py             # authored (default, offline)
    python scripts/record_push_gate.py --live groq # a real model, needs its key

Authored answers are each case's own reference SQL, marked "authored": true, as
the UI E2E's recordings are (tests/e2e/README.md): a steady fixture for checks
that are not about the model. A live recording stores what the model wrote,
right or wrong.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
STORE = os.path.join(ROOT, "tests", "evals", "recordings", "push_gate.json")
DATASETS = ("hospital", "fhir")


def _record_one(dataset: str, live: str | None) -> int:
    """Runs in a child process: the planner binds its schema at import time."""
    os.environ["EVAL_DATASET"] = dataset
    os.environ["ALLOW_REPLAY"] = "1"
    os.environ["E2E_RECORD"] = "1"
    os.environ["REPLAY_STORE"] = STORE
    os.environ.pop("REPLAY_SOURCE_PROVIDER", None)
    sys.path.insert(0, ROOT)
    os.chdir(ROOT)
    import eval_harness as eh   # sets PLANNER_SCHEMA for the dataset, then imports the planner
    from nl_to_sql_planner import CallMetrics, NLToSQLPlanner

    cases = [c for c in eh.CASES if c["id"] in eh.SUBSET_IDS]
    by_question = {c["question"]: c for c in cases}
    planner = NLToSQLPlanner(provider="replay")

    class Authored:
        """The case's reference SQL, through the same request path as a model."""

        def _complete(self, system, user, role):
            sql = by_question[user]["reference_sql"]
            return sql, CallMetrics(latency_ms=0, prompt_tokens=len(system) // 4,
                                    completion_tokens=len(sql) // 4, model="authored",
                                    provider="authored")

    planner._source = NLToSQLPlanner(provider=live, fallback="none") if live else Authored()
    before = set(planner._replay.entries)
    for c in cases:
        planner.generate_sql(c["question"])      # exactly what run_once asks
    new = set(planner._replay.entries) - before
    store = planner._replay
    for k in store.entries:
        if k in new:
            store.entries[k]["dataset"] = dataset
            store.entries[k]["case_id"] = by_question[store.entries[k]["user"]]["id"] \
                if store.entries[k]["user"] in by_question else None
            if not live:
                store.entries[k]["authored"] = True
    with open(store.path, "w", encoding="utf-8") as f:
        json.dump(store.entries, f, indent=2, sort_keys=True)
        f.write("\n")
    print(f"{dataset}: {len(cases)} cases, {len(new)} new recordings")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--live", metavar="PROVIDER",
                    help="record a real model's answers (e.g. groq); default: authored")
    ap.add_argument("--keep", action="store_true",
                    help="keep existing recordings (default: start from an empty store)")
    ap.add_argument("--dataset", choices=DATASETS, help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.dataset:
        return _record_one(args.dataset, args.live)
    if not args.keep and os.path.exists(STORE):
        os.remove(STORE)
    os.makedirs(os.path.dirname(STORE), exist_ok=True)
    for ds in DATASETS:
        cmd = [sys.executable, os.path.abspath(__file__), "--dataset", ds]
        if args.live:
            cmd += ["--live", args.live]
        subprocess.run(cmd, check=True, cwd=ROOT)
    print(f"-> {os.path.relpath(STORE, ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
