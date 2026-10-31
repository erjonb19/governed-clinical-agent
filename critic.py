"""
critic.py
=========
The critic step both graphs run between `plan` and `execute` (PLAN v2 item 4).

One extra model call reviews the proposed SQL against the question and returns
a verdict:

    approve       the SQL answers the question; go on to execute
    revise        it does not; re-plan with the critic's reason (costs an attempt)
    unavailable   the call failed or its output was not a verdict; go on to
                  execute (D4: fail OPEN to the guard -- the critic is a quality
                  step, security stays with sql_guard)

THE CRITIC CAN ONLY MAKE THINGS STRICTER. It never skips, overrides or informs
the guard: `execute` runs sql_guard and the policy engine on every attempt
exactly as before (hard rule 1). An "approve" from a prompt-injected critic
changes nothing about what is allowed to run.

Switches (environment):
    CRITIC_ENABLED=1   run the critic at all (default 1)
    CRITIC_ROUTING=1   let "revise" send the run back to plan (default 1). With 0
                       the verdict is still recorded -- trajectory, trace span,
                       audit log -- but every attempt goes on to execute. This is
                       the PLAN v2 section 10 fallback if the critic costs accuracy.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Optional

APPROVE, REVISE, UNAVAILABLE = "approve", "revise", "unavailable"

# The planner's honest refusal (build_system_prompt): SELECT '<why>' AS unanswerable.
# The API turns it into the "switch dataset" card. Reviewing it is pointless and
# harmful -- a critic that calls it "not an answer" would push the planner to
# invent one -- so it is approved without a model call.
_UNANSWERABLE = re.compile(r"^\s*SELECT\s+'(?:[^']|'')*'\s+AS\s+unanswerable\s*;?\s*$",
                           re.I | re.S)


def is_refusal(sql: Optional[str]) -> bool:
    return bool(sql and _UNANSWERABLE.match(sql))


def enabled() -> bool:
    return os.environ.get("CRITIC_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}


def routing() -> bool:
    return os.environ.get("CRITIC_ROUTING", "1").strip().lower() not in {"0", "false", "no", "off"}


def parse_verdict(text: Optional[str]) -> dict:
    """The verdict in `text`, or `unavailable` if there is no usable one.

    Tolerates code fences and prose around the JSON; insists on a JSON object
    whose verdict is exactly approve or revise.
    """
    if not text:
        return {"verdict": UNAVAILABLE, "reason": "critic returned no text", "issues": []}
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return {"verdict": UNAVAILABLE, "reason": "critic output was not JSON", "issues": []}
    try:
        obj = json.loads(m.group(0))
    except ValueError:
        return {"verdict": UNAVAILABLE, "reason": "critic output was not valid JSON", "issues": []}
    if not isinstance(obj, dict):
        return {"verdict": UNAVAILABLE, "reason": "critic output was not a JSON object", "issues": []}
    verdict = str(obj.get("verdict", "")).strip().lower()
    if verdict not in (APPROVE, REVISE):
        return {"verdict": UNAVAILABLE, "reason": f"critic verdict {verdict!r} is not approve/revise",
                "issues": []}
    issues = obj.get("issues") or []
    if not isinstance(issues, list):
        issues = [str(issues)]
    return {"verdict": verdict, "reason": str(obj.get("reason") or "")[:500],
            "issues": [str(i)[:200] for i in issues[:10]]}


def review(planner: Any, question: str, sql: str, schema_doc: Optional[str] = None) -> dict:
    """Run the critic. Never raises: any failure is the `unavailable` verdict.

    Returns {verdict, reason, issues, metrics} where metrics is the call's
    CallMetrics.as_dict(), or None when no call completed.
    """
    if is_refusal(sql):
        return {"verdict": APPROVE, "reason": "an unanswerable refusal; nothing to review",
                "issues": [], "metrics": None}
    try:
        text, metrics = planner.critique(question, sql, schema_doc=schema_doc)
        out = parse_verdict(text if isinstance(text, str) else None)
        out["metrics"] = metrics.as_dict() if metrics is not None else None
        return out
    except Exception as e:
        # A call that completed but was unusable (PlannerError) was still billed:
        # keep its metrics so the trajectory and the cost log count it.
        m = getattr(e, "metrics", None)
        return {"verdict": UNAVAILABLE, "reason": f"critic error: {type(e).__name__}: {e}"[:500],
                "issues": [], "metrics": m.as_dict() if m is not None else None}


def audit(runtime: Any, capability: str, verdict: dict, sql_sha256: Optional[str]) -> None:
    """Write the verdict to the runtime's audit log, beside the guard's decisions."""
    logger = getattr(runtime, "audit_logger", None)
    if logger is None or not hasattr(logger, "log_critic_review"):
        return
    try:
        logger.log_critic_review(capability=capability, verdict=verdict["verdict"],
                                 reason=verdict.get("reason") or "", sql_sha256=sql_sha256)
    except Exception:
        pass   # the audit line is best effort, like the tracer
