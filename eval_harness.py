"""
eval_harness.py
===============
The evaluation framework. Runs every case in eval_bank.py through the full
governed agent, multiple times each, and scores the results against trusted
reference SQL. Built to answer the question every AI-engineering role asks:
how do you know the agent is correct, and how consistently?

What it measures:
  - Accuracy: does the agent's answer match the reference exactly (per run)
  - Consistency: across N runs of the same question, how stable is it
    (LLMs are non-deterministic; a question that passes 1/3 is not "passing")
  - Failure modes: WHY a run failed, classified into a taxonomy
  - Per-tier accuracy: where the agent is strong vs. where it breaks down

Output:
  - A console report
  - A timestamped JSON in eval_runs/ for regression tracking over time
  - Exit code 0 only if accuracy meets THRESHOLD (a deployment eval gate)

Run from the repo root with the planner key set:
    python eval_harness.py
    python eval_harness.py --runs 5           # more rigorous
    python eval_harness.py --provider groq    # fallback provider when primary is down

Exit codes: 0 = pass, 1 = accuracy regression, 2 = provider unavailable
(inconclusive -- e.g. the LLM provider returned 402/429/5xx for most runs).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Load a local .env (if present) before reading os.environ. Optional dependency.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Two ground-truth banks, one per dataset. EVAL_DATASET selects which, and it
# MUST match the database the analytics tool is connected to -- a bank is only
# an answer key for the data it was written against.
_DATASET = os.environ.get("EVAL_DATASET", "hospital").lower()
if _DATASET == "fhir":
    _DEFAULT_DB = "medallion/fhir_gold.duckdb"
    _DEFAULT_SCHEMA = "fhir"
else:
    _DEFAULT_DB = "medallion/hospital_gold.duckdb"
    _DEFAULT_SCHEMA = "hospital"

# The dataset OWNS the planner schema, so ASSIGN it (never setdefault) and do it
# BEFORE importing the planner. nl_to_sql_planner binds SCHEMA_DOC/SYSTEM_PROMPT
# at IMPORT time, and a local .env commonly pins PLANNER_SCHEMA=hospital, which
# a later setdefault cannot override. Setting it any later silently planned FHIR
# questions against the hospital schema: that surfaces as sql_error and reads
# like an agent regression rather than the config bug it actually is.
os.environ["PLANNER_SCHEMA"] = _DEFAULT_SCHEMA

from src.runtime.agent_runtime import AgentRuntime            # noqa: E402
from analytics_query_tool import AnalyticsQueryTool           # noqa: E402
from nl_to_sql_planner import NLToSQLPlanner, PROVIDERS       # noqa: E402
from data_backends import LocalDuckDBBackend, get_backend     # noqa: E402
if _DATASET == "fhir":
    from eval_bank_fhir import CASES, SUBSET_IDS              # noqa: E402
else:
    from eval_bank import CASES, SUBSET_IDS                   # noqa: E402

GOLD_DB = os.environ.get("HOSPITAL_GOLD_DB", _DEFAULT_DB)
POLICY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "medicare_policy.yaml")
CAPABILITY = "analytics.query_aggregate"

DEFAULT_RUNS = 3
THRESHOLD = 0.80          # eval gate: accuracy over COMPLETED runs must meet this
OUT_DIR = "eval_runs"

# A gate verdict is only meaningful if enough runs actually REACHED the provider.
# Accuracy is scored over completed runs, so without a floor a sweep where most
# runs 429'd would report a confident number computed from the few that got
# through -- e.g. 49% provider errors would "pass" on barely half the suite.
# Below this fraction the run is inconclusive ("provider unavailable") rather
# than a pass or a false accuracy regression.
MIN_COMPLETED_FRACTION = 0.80

# Stop calling a provider that is plainly down. After this many CONSECUTIVE
# provider errors, the remaining runs are recorded as provider errors WITHOUT
# being attempted. The verdict is already provider_unavailable at that point, so
# grinding through the rest only burns wall-clock and pacing delay -- four FHIR
# graph attempts on 2026-09-05 each spent ~7 minutes making 84 doomed calls
# against an exhausted daily quota to report what the first dozen already showed.
ABORT_AFTER_CONSECUTIVE_PROVIDER_ERRORS = 12

# Exit codes -- distinct so CI can tell a real regression from an infra outage.
EXIT_OK = 0
EXIT_REGRESSION = 1
EXIT_PROVIDER_UNAVAILABLE = 2

# failure taxonomy
F_CORRECT = "correct"
F_WRONG_ANSWER = "wrong_answer"       # ran fine, result disagrees with reference
F_MISSING_KEY = "missing_key_column"  # agent didn't return the column asked for
F_GUARD_DENIED = "guard_denied"       # guard rejected the SQL
F_SQL_ERROR = "sql_error"             # SQL ran but errored (bad column, etc.)
F_MODEL_ERROR = "model_error"         # planner/model call failed (genuine logic error)
F_PROVIDER_ERROR = "provider_error"   # infra: 402/429/5xx, network, timeout -- NOT a regression

# Infrastructure/transport failures. A planner call that dies for one of these
# reasons tells us nothing about agent accuracy, so it must be scored apart from
# a real regression (a wrong or guard-denied answer).
_PROVIDER_STATUS_CODES = {402, 408, 425, 429, 500, 502, 503, 504}
_PROVIDER_EXC_NAMES = {
    "RateLimitError", "InternalServerError", "APITimeoutError",
    "APIConnectionError", "APIStatusError",
}


def _is_provider_error(exc: Exception) -> bool:
    """True when a planner call failed for an infrastructure reason -- payment
    (402), rate limit (429), server (5xx), network, or timeout -- rather than a
    genuine model/planner logic error (e.g. a 400 bad request).

    Checks, in order: an SDK status_code, the exception class name, then a
    message sniff as a last resort so it still works if the error is wrapped.
    """
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and (status in _PROVIDER_STATUS_CODES or status >= 500):
        return True
    if type(exc).__name__ in _PROVIDER_EXC_NAMES:
        return True
    msg = str(exc).lower()
    if re.search(r"error code:\s*(402|408|425|429|5\d\d)", msg):
        return True
    # Billing/quota failures are availability problems, not accuracy regressions,
    # even when a provider returns them as 403 (e.g. xAI: "team doesn't have any
    # credits or licenses yet") rather than 402. A bare permission/model-access
    # 403 with none of these terms stays a real error (a config bug to fix).
    return any(s in msg for s in (
        "payment required", "insufficient", "quota", "rate limit",
        "credit", "license", "billing", "no active subscription",
        "service unavailable", "bad gateway", "gateway timeout",
        "overloaded", "connection error", "timed out",
    ))


def _scalar(rows):
    """Pull the answer value from a scalar result.

    A strict scalar query returns one column. But the agent often returns a row
    like (facility_name, measure) for a 'what is the highest X' question -- the
    ANSWER is the measure, which lands LAST, not first. So: if the single row has
    multiple columns, prefer the last numeric column; fall back to the last
    column; only use the first when there is just one.
    """
    if not rows:
        return None
    row = rows[0]
    vals = list(row.values())
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    # multiple columns: the measure is the answer. Prefer the last numeric value.
    for v in reversed(vals):
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return v
    return vals[-1]


def _keys(rows, key_columns):
    out = []
    for r in rows:
        if not all(k in r for k in key_columns):
            return None
        out.append(tuple(r[k] for k in key_columns))
    return out


def score_one(case, agent_rows) -> tuple[str, str]:
    """Return (failure_mode, detail). F_CORRECT means it matched."""
    # reference is computed once by the caller and passed via case['_ref_rows']
    ref_rows = case["_ref_rows"]
    if case["mode"] == "scalar":
        a, b = _scalar(agent_rows), _scalar(ref_rows)
        try:
            ok = (a == b) or (a is not None and b is not None and float(a) == float(b))
        except (TypeError, ValueError):
            ok = (a == b)
        return (F_CORRECT, f"{a}") if ok else (F_WRONG_ANSWER, f"agent={a} ref={b}")
    kc = case["key_columns"]
    a = _keys(agent_rows, kc)
    b = _keys(ref_rows, kc)
    if a is None:
        cols = list(agent_rows[0].keys()) if agent_rows else []
        return F_MISSING_KEY, f"missing {kc}; got {cols}"
    if a == b:
        return F_CORRECT, f"{len(a)} rows"
    return F_WRONG_ANSWER, f"agent={a[:3]}... ref={b[:3]}..."


def run_once(case, runtime, planner) -> tuple[str, str]:
    """One agent attempt at one case. Returns (failure_mode, detail)."""
    try:
        sql = planner.generate_sql(case["question"])
    except Exception as e:
        mode = F_PROVIDER_ERROR if _is_provider_error(e) else F_MODEL_ERROR
        return mode, str(e)[:120]
    result = runtime.execute_tool(CAPABILITY, {"sql": sql})
    if not getattr(result, "allowed", False):
        reason = getattr(result, "explanation", "") or ""
        mode = F_GUARD_DENIED if ("guard" in reason.lower() or "sql_guard" in reason) else F_GUARD_DENIED
        return mode, reason[:120]
    tr = result.result
    if tr is None:
        return F_SQL_ERROR, "no tool result"
    if not tr.success:
        err = (tr.error or "")[:120]
        mode = F_GUARD_DENIED if "guard" in err.lower() else F_SQL_ERROR
        return mode, err
    return score_one(case, (tr.output or {}).get("rows", []))


def run_once_graph(case, agent):
    """One attempt at one case THROUGH THE GRAPH (self-correcting, bounded retries).

    Scoring is identical to the single-shot path -- same score_one() -- so the
    comparison is apples-to-apples. Only the execution path differs.
    """
    try:
        state = agent.run(case["question"])
    except Exception as e:
        mode = F_PROVIDER_ERROR if _is_provider_error(e) else F_MODEL_ERROR
        return mode, str(e)[:120], 1
    attempts = state.get("attempts", 1)
    if not state.get("allowed"):
        reason = str(state.get("reason") or "denied")
        mode = F_GUARD_DENIED if "guard" in reason.lower() else F_SQL_ERROR
        return mode, reason[:120], attempts
    fmode, detail = score_one(case, state.get("rows") or [])
    return fmode, detail, attempts


def _gate_status(correct_runs: int, total_runs: int, provider_error_runs: int):
    """Decide a run's verdict, keeping infrastructure separate from accuracy.

    Returns (status, completed_accuracy, completed_runs) where status is one of
    "pass", "fail" or "provider_unavailable".

    Accuracy is scored over runs that actually executed -- a 429 says nothing
    about correctness, so leaving provider errors in the denominator turns a
    partial outage into a phantom regression. The flip side is that a thin
    sample must not masquerade as a confident verdict, so a minimum share of
    the planned runs has to have completed before the gate will rule at all.
    """
    completed_runs = total_runs - provider_error_runs
    completed_accuracy = (correct_runs / completed_runs) if completed_runs else 0.0
    if total_runs <= 0 or completed_runs < MIN_COMPLETED_FRACTION * total_runs:
        return "provider_unavailable", completed_accuracy, completed_runs
    if completed_accuracy >= THRESHOLD:
        return "pass", completed_accuracy, completed_runs
    return "fail", completed_accuracy, completed_runs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=DEFAULT_RUNS)
    ap.add_argument("--graph", action="store_true",
                    help="route through the LangGraph self-correcting agent "
                         "instead of the single-shot planner")
    ap.add_argument("--provider", default=os.environ.get("PLANNER_PROVIDER"),
                    help="planner LLM provider (e.g. cerebras, groq, xai, anthropic). "
                         "Defaults to $PLANNER_PROVIDER, else the planner's built-in "
                         "default. Use this to run against a FALLBACK provider when "
                         "the primary is unavailable (needs that provider's API key).")
    ap.add_argument("--min-interval", type=float, default=None,
                    help="minimum seconds between planner API calls (rate-limit "
                         "pacing for free/low tiers). Also settable via "
                         "PLANNER_MIN_INTERVAL_SEC.")
    args = ap.parse_args()
    provider = args.provider
    if provider and provider not in PROVIDERS:
        sys.exit(f"unknown --provider '{provider}'. choose from: "
                 f"{', '.join(sorted(PROVIDERS))}")
    if args.min_interval is not None:
        os.environ["PLANNER_MIN_INTERVAL_SEC"] = str(args.min_interval)
    runs = args.runs
    # env overrides for CI: EVAL_RUNS sets runs, EVAL_SUBSET=1 runs the quick subset
    if os.environ.get('EVAL_RUNS'):
        try:
            runs = int(os.environ['EVAL_RUNS'])
        except ValueError:
            pass
    active_cases = CASES
    if os.environ.get('EVAL_SUBSET') == '1':
        active_cases = [c for c in CASES if c['id'] in SUBSET_IDS]

    if not os.path.exists(GOLD_DB):
        sys.exit(f"missing {GOLD_DB} -- build the hospital Gold first")

    # The tool registry is global: a capability can only be registered once per
    # process. In graph mode the GovernedAgentGraph builds its OWN runtime and
    # registers the tool itself, so we must NOT also build the single-shot one.
    graph_agent = None
    runtime = None
    if args.graph:
        from agent_graph import GovernedAgentGraph
        graph_agent = GovernedAgentGraph(db_path=GOLD_DB, provider=provider)
    else:
        runtime = AgentRuntime()
        runtime.load_policy(POLICY_PATH)
        runtime.register_tool(AnalyticsQueryTool(db_path=GOLD_DB, seed_demo=False))
    planner = NLToSQLPlanner(provider=provider) if provider else NLToSQLPlanner()
    ref_backend = LocalDuckDBBackend(GOLD_DB)

    # precompute reference answers once
    for case in active_cases:
        _, case["_ref_rows"] = ref_backend.execute(case["reference_sql"])

    backend_kind = get_backend(GOLD_DB).kind
    mode_label = "GRAPH (self-correcting)" if args.graph else "SINGLE-SHOT"
    print(f"eval: {len(active_cases)} cases x {runs} runs | dataset={_DATASET} | path={mode_label} | "
          f"backend={backend_kind} | planner={planner.provider} ({planner._model})\n")

    case_results = []
    tier_totals = defaultdict(lambda: [0, 0])   # tier -> [correct_runs, total_runs]
    failure_counts = defaultdict(int)
    provider_error_sample = ""
    t_start = time.time()

    consecutive_provider_errors = 0
    provider_down = False

    for case in active_cases:
        outcomes = []
        details = []
        attempts_used = []
        for _ in range(runs):
            if provider_down:
                # Provider already proven unreachable -- record the run without
                # attempting it, so the totals stay consistent and the report is
                # still a complete, correctly-shaped provider_unavailable.
                mode, detail = F_PROVIDER_ERROR, "not attempted: provider unreachable"
                attempts_used.append(1)
            elif graph_agent is not None:
                mode, detail, n_att = run_once_graph(case, graph_agent)
                attempts_used.append(n_att)
            else:
                mode, detail = run_once(case, runtime, planner)
                attempts_used.append(1)
            outcomes.append(mode)
            details.append(detail)
            failure_counts[mode] += 1
            if mode == F_PROVIDER_ERROR and not provider_error_sample:
                provider_error_sample = detail
            tier_totals[case["tier"]][1] += 1
            if mode == F_CORRECT:
                tier_totals[case["tier"]][0] += 1
            if mode == F_PROVIDER_ERROR:
                consecutive_provider_errors += 1
                if (not provider_down and consecutive_provider_errors
                        >= ABORT_AFTER_CONSECUTIVE_PROVIDER_ERRORS):
                    provider_down = True
                    print()
                    print(f"  [abort] {consecutive_provider_errors} consecutive "
                          f"provider errors -- the provider is down; skipping the "
                          f"remaining runs instead of retrying each one.")
                    print()
            else:
                consecutive_provider_errors = 0
        correct = sum(1 for o in outcomes if o == F_CORRECT)
        stability = correct / runs
        # a case "passes" only if it is correct on the majority of runs
        passed = correct > runs / 2
        case_results.append({
            "id": case["id"], "tier": case["tier"], "question": case["question"],
            "correct_runs": correct, "runs": runs, "stability": round(stability, 2),
            "passed": passed, "outcomes": outcomes, "sample_detail": details[0],
            "avg_attempts": round(sum(attempts_used) / len(attempts_used), 2),
        })
        flag = "PASS" if passed else "FAIL"
        bar = "".join("O" if o == F_CORRECT else "x" for o in outcomes)
        print(f"[{flag}] t{case['tier']} {case['id']:26} {bar}  ({correct}/{runs})  {details[0][:50]}")

    elapsed = time.time() - t_start
    n_pass = sum(1 for c in case_results if c["passed"])
    total_runs = len(active_cases) * runs
    correct_runs = sum(c["correct_runs"] for c in case_results)
    run_accuracy = correct_runs / total_runs
    # The verdict, decided once here and reused by the report, the banner and the
    # exit code. Accuracy is scored over runs that ACTUALLY EXECUTED -- provider
    # errors (429/5xx/no credits) say nothing about correctness, so leaving them
    # in the denominator makes a partial outage look like a regression -- while
    # the completed-runs floor inside _gate_status keeps a thin sample from
    # being reported as a confident pass.
    provider_error_runs = failure_counts.get(F_PROVIDER_ERROR, 0)
    status, completed_accuracy, completed_runs = _gate_status(
        correct_runs, total_runs, provider_error_runs)
    case_pass_rate = n_pass / len(active_cases)

    print("\n" + "=" * 60)
    print(f"cases passed (majority-correct): {n_pass}/{len(active_cases)}  ({case_pass_rate:.0%})")
    print(f"run-level accuracy:              {correct_runs}/{total_runs}  ({run_accuracy:.0%})")
    if provider_error_runs:
        print(f"accuracy among completed runs:   {correct_runs}/{completed_runs}  "
              f"({completed_accuracy:.0%})   [{provider_error_runs} runs never reached the provider]")
    print("per tier (run-level):")
    for tier in sorted(tier_totals):
        c, t = tier_totals[tier]
        print(f"  tier {tier}: {c}/{t}  ({c/t:.0%})")
    print("failure modes:")
    for mode, n in sorted(failure_counts.items(), key=lambda x: -x[1]):
        if mode != F_CORRECT:
            print(f"  {mode}: {n}")
    if graph_agent is not None:
        all_att = [c["avg_attempts"] for c in case_results]
        retried = [c for c in case_results if c["avg_attempts"] > 1.0]
        print(f"self-correction: {len(retried)}/{len(case_results)} cases needed a retry "
              f"(avg attempts {sum(all_att)/len(all_att):.2f})")
        for c in retried:
            print(f"    {c['id']}: avg {c['avg_attempts']} attempts, passed={c['passed']}")
    print(f"elapsed: {elapsed:.0f}s")

    # write timestamped report for regression tracking
    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    provider_outage = status == "provider_unavailable"

    report = {
        "timestamp": stamp,
        "dataset": _DATASET,
        "path": "graph" if args.graph else "single_shot",
        "backend": backend_kind,
        "provider": planner.provider,
        "model": planner._model,
        "status": status,
        "runs_per_case": runs,
        "case_pass_rate": round(case_pass_rate, 4),
        "run_accuracy": round(run_accuracy, 4),
        "completed_accuracy": round(completed_accuracy, 4),
        "completed_runs": completed_runs,
        "provider_error_runs": provider_error_runs,
        "tier_accuracy": {str(t): round(v[0]/v[1], 4) for t, v in sorted(tier_totals.items())},
        "failure_counts": dict(failure_counts),
        "cases": case_results,
    }
    tag = ("graph" if args.graph else "single") + "_" + _DATASET
    path = os.path.join(OUT_DIR, f"eval_{tag}_{stamp}.json")
    with open(path, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\nreport -> {path}")

    # Infrastructure outage: report it explicitly and exit with a DISTINCT code so
    # CI does not mistake a provider failure for an accuracy regression.
    if provider_outage:
        print("\n" + "!" * 64)
        print("PROVIDER UNAVAILABLE -- eval INCONCLUSIVE (not an accuracy regression)")
        print(f"  {provider_error_runs}/{total_runs} runs failed with a provider/infra "
              f"error (402/403-no-credits/429/5xx/network)")
        print(f"  only {completed_runs}/{total_runs} runs reached the provider; a verdict "
              f"needs >= {MIN_COMPLETED_FRACTION:.0%}")
        print(f"  provider: {planner.provider} ({planner._model})")
        if provider_error_sample:
            print(f"  sample:   {provider_error_sample[:80]}")
        print("  The accuracy above is meaningless under an outage and is NOT a gate failure.")
        print("  Retry when the provider recovers, or run against a fallback provider:")
        print("    python eval_harness.py --provider groq        # needs GROQ_API_KEY")
        print("    python eval_harness.py --provider anthropic   # needs ANTHROPIC_API_KEY")
        print("!" * 64)
        print("eval gate: SKIPPED (provider unavailable)")
        sys.exit(EXIT_PROVIDER_UNAVAILABLE)

    # eval gate
    gate = "PASS" if status == "pass" else "FAIL"
    print(f"eval gate (>= {THRESHOLD:.0%} of completed runs): {gate}")
    sys.exit(EXIT_OK if status == "pass" else EXIT_REGRESSION)


if __name__ == "__main__":
    main()
