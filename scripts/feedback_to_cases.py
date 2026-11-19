"""Turn every thumbs-down into a candidate eval case for the owner to finish (item 24).

For each answer rated down (feedback.FeedbackStore), read its trace and write
one JSON file under logs/eval_candidates/ (gitignored, like the rest of logs/):
the question, the dataset, the SQL the agent ran, what the guard decided, the
rater's reason and a link to the trace. `reference_sql`, `mode` and `tier` are
left as TODO: only a person can say what the right answer is. Once finished, the
case goes into eval_bank.py or eval_bank_fhir.py by hand, through a PR.

Run it where the traces are: ratings can live in Postgres (STATE_DB_URL), but
traces are always the server's own SQLite file (TRACE_DB), so on another machine
every candidate comes out as "trace gone". A candidate whose trace is gone (a
Render restart wipes traces) is written with what is known and says so. Existing files are left alone, so the script can run
again safely.

    python scripts/feedback_to_cases.py [--out logs/eval_candidates] [--base-url URL]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Optional

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)


def _last_attr(spans: list[dict], name: str, attr: str) -> Optional[Any]:
    for span in reversed(spans):
        if span.get("name") == name and span.get("attrs", {}).get(attr) is not None:
            return span["attrs"][attr]
    return None


def candidate(
    trace_id: str, reason: Optional[str], rated_at: str, trace: Optional[dict], base_url: str
) -> dict:
    attrs = (trace or {}).get("attrs", {})
    spans = (trace or {}).get("spans", [])
    return {
        "id": f"cand_{trace_id[:12]}",
        "status": "candidate: finish reference_sql, mode and tier, then add it to the bank",
        "dataset": attrs.get("dataset"),
        "question": attrs.get("question"),
        "agent_sql": _last_attr(spans, "plan", "sql") or attrs.get("sql"),
        "decided_by": attrs.get("decided_by") or _last_attr(spans, "guard", "decided_by"),
        "rater_reason": reason,
        "rated_at": rated_at,
        "trace": f"{base_url.rstrip('/')}/traces/{trace_id}",
        "trace_found": trace is not None,
        "reference_sql": "TODO",
        "mode": "TODO",
        "tier": "TODO",
    }


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default=os.path.join("logs", "eval_candidates"))
    ap.add_argument("--base-url", default="", help="prefix for the trace link, e.g. the deploy URL")
    args = ap.parse_args(argv)

    import feedback
    import tracing

    tracer = tracing.get_tracer()
    os.makedirs(args.out, exist_ok=True)
    written = 0
    for row in feedback.FeedbackStore().rated_down():
        path = os.path.join(args.out, f"cand_{row['trace_id']}.json")
        if os.path.exists(path):
            continue
        trace = tracer.get_trace(row["trace_id"])
        case = candidate(row["trace_id"], row.get("reason"), row["rated_at"], trace, args.base_url)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(case, f, indent=2, ensure_ascii=False)
            f.write("\n")
        written += 1
        print(f"{path}  ({'trace found' if trace else 'trace gone'})")
    print(f"{written} new candidate case(s) in {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
