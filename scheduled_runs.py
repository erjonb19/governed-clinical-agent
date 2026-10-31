"""
scheduled_runs.py
=================
Standing questions, run unattended (PLAN v2 7e; the operations track's O3).

Every schedule in schedules.yaml names an eval case. This script runs each one
that is due through the self-correcting graph, headless, and records the
outcome in logs/scheduled_runs.sqlite:

    ok              answered, matches the reference, same answer as last run
    drift           answered, matches the reference, but the answer CHANGED
                    since the last recorded run (the data moved: a CMS refresh)
    failed          guard denial, SQL error, model error, or a wrong answer
    provider_error  the LLM provider was unreachable (402/429/5xx) -- infra,
                    not a regression, the same split eval_harness.py makes
    skipped         not started: its worst-case cost would break a cap

The answer is hashed in the form the eval scores it (the scalar value, or the
key columns in order), so an aliased column name is not drift.

Exit codes follow eval_harness.py, plus D9's budget stop:
    0 every run ok or drift      1 a run failed
    2 provider unavailable       3 a cost cap stopped a run

    python scheduled_runs.py                      # due hospital schedules
    python scheduled_runs.py --all --summary s.json
    python scheduled_runs.py --file-issues s.json # CI: open/update/close issues

Nothing here touches the guard: every question goes through
GovernedAgentGraph, the same governed runtime /query and the evals use.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from typing import Any, Callable, Optional

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

SCHEDULES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schedules.yaml")
DB_PATH = os.environ.get("SCHEDULED_RUNS_DB", "logs/scheduled_runs.sqlite")

DATASETS = ("hospital", "fhir")
CADENCE_DAYS = {"weekly": 7, "monthly": 28}
# A weekly cron fires every 7 days to the minute, give or take queueing; a day
# of slack keeps a run that started a little early from being "not yet due".
CADENCE_SLACK = dt.timedelta(days=1)

# Worst-case prompt size for one planner call. Planner prompts are ~1.5-2K
# tokens (PLAN v2 section 8); twice that so the estimate stays an upper bound.
PROMPT_TOKENS_WORST = 4000
# The OpenAI-compatible path sends max_tokens=800; the Anthropic path carries
# its own (_max_tokens).
DEFAULT_MAX_OUTPUT_TOKENS = 800

OK, DRIFT, FAILED, PROVIDER_ERROR, SKIPPED = "ok", "drift", "failed", "provider_error", "skipped"
EXIT_OK, EXIT_FAILED, EXIT_PROVIDER_UNAVAILABLE, EXIT_BUDGET = 0, 1, 2, 3

# Datasets whose answer values may appear in a result's detail. The detail goes
# into the summary artifact and the GitHub issue, and the repo is public: CMS
# hospital data is public, FHIR answers are patient-level, so for FHIR only the
# failure mode is kept (hard rule 3).
DETAIL_VALUES_OK = {"hospital"}

ISSUE_LABEL = "scheduled-run"
ISSUE_PREFIX = "scheduled run: "


class ScheduleError(ValueError):
    """schedules.yaml is malformed or names a case that is not in its bank."""


# ---------------------------------------------------------------------------
# schedules.yaml
# ---------------------------------------------------------------------------

def bank_cases(dataset: str) -> dict:
    """{case id: case} for a dataset's eval bank. Plain modules, no side effects."""
    if dataset == "fhir":
        from eval_bank_fhir import CASES
    else:
        from eval_bank import CASES
    return {c["id"]: c for c in CASES}


def load_schedules(path: str = SCHEDULES_PATH) -> dict:
    """Parse and validate schedules.yaml. Raises ScheduleError on any problem."""
    with open(path, encoding="utf-8") as f:
        doc = yaml.safe_load(f) or {}
    budget = doc.get("budget_usd")
    if not isinstance(budget, (int, float)) or isinstance(budget, bool) or budget <= 0:
        raise ScheduleError("budget_usd must be a positive number")
    items = doc.get("schedules")
    if not isinstance(items, list) or not items:
        raise ScheduleError("schedules must be a non-empty list")
    banks = {d: bank_cases(d) for d in DATASETS}
    seen = set()
    for s in items:
        if not isinstance(s, dict):
            raise ScheduleError(f"schedule entry is not a mapping: {s!r}")
        name = s.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ScheduleError(f"schedule has no name: {s!r}")
        if name in seen:
            raise ScheduleError(f"duplicate schedule name: {name}")
        seen.add(name)
        if s.get("dataset") not in DATASETS:
            raise ScheduleError(f"{name}: dataset must be one of {DATASETS}")
        if s.get("case") not in banks[s["dataset"]]:
            raise ScheduleError(f"{name}: case {s.get('case')!r} is not in the "
                                f"{s['dataset']} eval bank -- add it as an eval case first")
        if s.get("cadence") not in CADENCE_DAYS:
            raise ScheduleError(f"{name}: cadence must be one of {sorted(CADENCE_DAYS)}")
        cap = s.get("max_cost_usd")
        if not isinstance(cap, (int, float)) or isinstance(cap, bool) or cap <= 0:
            raise ScheduleError(f"{name}: max_cost_usd must be a positive number")
    return {"budget_usd": float(budget), "schedules": items}


# ---------------------------------------------------------------------------
# answers and cost
# ---------------------------------------------------------------------------

def canonical_answer(case: dict, rows: list) -> Any:
    """The answer in the form the eval scores it, so hashing ignores aliases."""
    from eval_harness import _keys, _scalar
    if case["mode"] == "scalar":
        v = _scalar(rows)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return round(float(v), 6)    # 748 and 748.0 are the same answer
        return v
    return _keys(rows, case["key_columns"])


def answer_hash(answer: Any) -> str:
    blob = json.dumps(answer, default=str, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def worst_case_usd(planner: Any, max_attempts: int) -> float:
    """The most one question can cost: every attempt at full prompt and output."""
    from nl_to_sql_planner import rate_for
    rate = rate_for(planner.provider, planner._model)
    out = getattr(planner, "_max_tokens", DEFAULT_MAX_OUTPUT_TOKENS)
    per_call = (PROMPT_TOKENS_WORST * rate["input"] + out * rate["output"]) / 1_000_000
    return max_attempts * per_call


# ---------------------------------------------------------------------------
# result store
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at       TEXT NOT NULL,          -- ISO-8601 UTC
    schedule     TEXT NOT NULL,
    case_id      TEXT NOT NULL,
    dataset      TEXT NOT NULL,
    status       TEXT NOT NULL,
    result_hash  TEXT,                   -- NULL unless the answer was correct
    prev_hash    TEXT,
    trace_id     TEXT,
    attempts     INTEGER,
    cost_usd     REAL,
    detail       TEXT
);
CREATE INDEX IF NOT EXISTS runs_schedule ON runs (schedule, id);
"""


class ResultStore:
    """One row per scheduled run. Only hashes and counts, never result rows."""

    def __init__(self, path: str = DB_PATH):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with self._con() as con:
            con.executescript(_SCHEMA)

    @contextlib.contextmanager
    def _con(self):
        # sqlite3's own context manager commits but never closes, which leaves
        # the file locked on Windows; this one does both.
        con = sqlite3.connect(self.path)
        try:
            yield con
            con.commit()
        finally:
            con.close()

    def last_hash(self, schedule: str) -> Optional[str]:
        """The answer hash of the latest run that produced a correct answer."""
        with self._con() as con:
            row = con.execute(
                "SELECT result_hash FROM runs WHERE schedule = ? AND result_hash IS NOT NULL"
                " ORDER BY id DESC LIMIT 1", (schedule,)).fetchone()
        return row[0] if row else None

    def last_run_at(self, schedule: str) -> Optional[dt.datetime]:
        """When the schedule last got an answer judged. A skipped run or a
        provider outage does not count, or a monthly question would wait
        another 28 days after one."""
        with self._con() as con:
            row = con.execute(
                "SELECT run_at FROM runs WHERE schedule = ? AND status IN (?, ?, ?)"
                " ORDER BY id DESC LIMIT 1", (schedule, OK, DRIFT, FAILED)).fetchone()
        return dt.datetime.fromisoformat(row[0]) if row else None

    def record(self, r: dict) -> None:
        with self._con() as con:
            con.execute(
                "INSERT INTO runs (run_at, schedule, case_id, dataset, status, result_hash,"
                " prev_hash, trace_id, attempts, cost_usd, detail)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (r["run_at"], r["schedule"], r["case"], r["dataset"], r["status"],
                 r.get("result_hash"), r.get("prev_hash"), r.get("trace_id"),
                 r.get("attempts"), r.get("cost_usd"), r.get("detail")))


def is_due(schedule: dict, last: Optional[dt.datetime], now: dt.datetime) -> bool:
    if last is None:
        return True
    return now - last >= dt.timedelta(days=CADENCE_DAYS[schedule["cadence"]]) - CADENCE_SLACK


# ---------------------------------------------------------------------------
# running
# ---------------------------------------------------------------------------

def run_one(schedule: dict, case: dict, agent: Any, prev_hash: Optional[str],
            is_provider_error: Callable[[Exception], bool]) -> dict:
    """Ask one standing question through the graph and classify the outcome.

    `case` carries `_ref_rows`, the reference answer computed by the caller.
    """
    out = _run_one(case, agent, prev_hash, is_provider_error)
    kind = out.pop("kind", None)
    if schedule["dataset"] not in DETAIL_VALUES_OK:
        # Rebuilt from the classification alone: no text the answer could ride
        # in on, and no hashes (a small count is recoverable from its hash).
        out["detail"] = f"{out['status']}" + (f" ({kind})" if kind else "") + ": values withheld"
    return out


def _run_one(case: dict, agent: Any, prev_hash: Optional[str],
             is_provider_error: Callable[[Exception], bool]) -> dict:
    from eval_harness import F_CORRECT, score_one
    out: dict = {"prev_hash": prev_hash}
    try:
        state = agent.run(case["question"])
    except Exception as e:
        # The run may have paid for several calls before it raised, and its
        # state (with the cost) is lost with the exception. Charge the worst
        # case, so the sweep budget stays a bound.
        out.update(status=PROVIDER_ERROR if is_provider_error(e) else FAILED,
                   kind=type(e).__name__, detail=f"{type(e).__name__}: {e}"[:300],
                   cost_usd=worst_case_usd(agent.planner, agent.max_attempts))
        return out
    out.update(trace_id=state.get("trace_id"), attempts=state.get("attempts"),
               cost_usd=float(state.get("total_cost_usd") or 0.0))
    if not state.get("allowed"):
        out.update(status=FAILED, kind=state.get("decided_by") or "denied",
                   detail=f"{state.get('decided_by') or 'denied'}: {state.get('reason') or ''}"[:300])
        return out
    rows = state.get("rows") or []
    mode, detail = score_one(case, rows)
    if mode != F_CORRECT:
        out.update(status=FAILED, kind=mode, detail=f"{mode}: {detail}"[:300])
        return out
    h = answer_hash(canonical_answer(case, rows))
    changed = prev_hash is not None and h != prev_hash
    out.update(status=DRIFT if changed else OK, result_hash=h,
               detail=(f"answer changed since the last run ({prev_hash} -> {h}): {detail}"
                       if changed else detail)[:300])
    return out


def run_schedules(config: dict, cases: dict, agent: Any, store: ResultStore,
                  is_provider_error: Callable[[Exception], bool],
                  now: Optional[dt.datetime] = None, run_all: bool = False,
                  dataset: str = "hospital") -> list[dict]:
    """Run every due schedule for `dataset`, within the caps. Returns the results."""
    now = now or dt.datetime.now(dt.timezone.utc)
    worst = worst_case_usd(agent.planner, agent.max_attempts)
    spent = 0.0
    results = []
    for s in config["schedules"]:
        if s["dataset"] != dataset:
            continue
        if not run_all and not is_due(s, store.last_run_at(s["name"]), now):
            continue
        base = {"run_at": now.isoformat(timespec="seconds"), "schedule": s["name"],
                "case": s["case"], "dataset": s["dataset"]}
        if worst > s["max_cost_usd"]:
            r = {**base, "status": SKIPPED, "cost_usd": 0.0,
                 "detail": f"worst case ${worst:.4f} exceeds this schedule's cap ${s['max_cost_usd']}"}
        elif spent + worst > config["budget_usd"]:
            r = {**base, "status": SKIPPED, "cost_usd": 0.0,
                 "detail": (f"worst case ${worst:.4f} would take the sweep past "
                            f"budget_usd ${config['budget_usd']} (spent ${spent:.4f})")}
        else:
            r = {**base, **run_one(s, cases[s["case"]], agent, store.last_hash(s["name"]),
                                   is_provider_error)}
            spent += r.get("cost_usd") or 0.0
        store.record(r)
        results.append(r)
    return results


def exit_code(results: list[dict]) -> int:
    statuses = {r["status"] for r in results}
    if FAILED in statuses:
        return EXIT_FAILED
    if SKIPPED in statuses:
        return EXIT_BUDGET
    if PROVIDER_ERROR in statuses:
        return EXIT_PROVIDER_UNAVAILABLE
    return EXIT_OK


# ---------------------------------------------------------------------------
# GitHub issues
# ---------------------------------------------------------------------------

def issue_actions(results: list[dict], open_issues: dict[str, int],
                  run_url: str = "") -> list[dict]:
    """What to do on GitHub, given this sweep and the open issues {title: number}.

    One issue per schedule. failed or drift opens it, or comments on it if it is
    already open; the next ok run comments and closes it. A provider outage or a
    skipped run changes nothing: neither says anything about the answer.
    """
    actions = []
    link = f"\n\nRun: {run_url}" if run_url else ""
    for r in results:
        title = ISSUE_PREFIX + r["schedule"]
        number = open_issues.get(title)
        body = (f"**{r['status']}** on {r['run_at']} -- case `{r['case']}` ({r['dataset']})\n\n"
                f"{r.get('detail') or ''}\n\ntrace: `{r.get('trace_id')}`  attempts: "
                f"{r.get('attempts') or 'unknown'}  cost: ${r.get('cost_usd') or 0:.4f}{link}")
        if r["status"] in (FAILED, DRIFT):
            if number:
                actions.append({"action": "comment", "number": number, "body": body})
            else:
                actions.append({"action": "open", "title": title, "body": body})
        elif r["status"] == OK and number:
            actions.append({"action": "close", "number": number,
                            "body": f"Recovered: {body}"})
    return actions


def open_issues_by_title(listed: list[dict]) -> dict[str, int]:
    """{title: number} of the open scheduled-run issues, whatever their labels.

    If two are open under one title, the oldest (lowest number) is the one we
    keep updating, so the thread stays in one place.
    """
    out: dict[str, int] = {}
    for i in listed:
        t = i.get("title") or ""
        if t.startswith(ISSUE_PREFIX) and (t not in out or i["number"] < out[t]):
            out[t] = i["number"]
    return out


def _gh(*args: str) -> str:
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def file_issues(summary_path: str) -> None:
    """Apply issue_actions() with the gh CLI. Needs GH_TOKEN with issues: write."""
    with open(summary_path, encoding="utf-8") as f:
        summary = json.load(f)
    _gh("label", "create", ISSUE_LABEL, "--force", "--color", "D93F0B",
        "--description", "Opened by the scheduled-runs workflow")
    # Find our issues by TITLE, not by label: a label removed by hand would hide
    # the open issue and the next failure would open a duplicate.
    listed = json.loads(_gh("issue", "list", "--state", "open",
                            "--limit", "1000", "--json", "number,title"))
    open_issues = open_issues_by_title(listed)
    run_url = ""
    if os.environ.get("GITHUB_RUN_ID"):
        run_url = (f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/"
                   f"{os.environ.get('GITHUB_REPOSITORY')}/actions/runs/{os.environ['GITHUB_RUN_ID']}")
    for a in issue_actions(summary["results"], open_issues, run_url):
        if a["action"] == "open":
            _gh("issue", "create", "--title", a["title"], "--body", a["body"],
                "--label", ISSUE_LABEL)
        elif a["action"] == "comment":
            _gh("issue", "comment", str(a["number"]), "--body", a["body"])
        elif a["action"] == "close":
            _gh("issue", "close", str(a["number"]), "--comment", a["body"])
        print(f"issue {a['action']}: {a.get('title') or '#' + str(a.get('number'))}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _markdown(results: list[dict], code: int) -> str:
    lines = [f"### Scheduled runs (exit {code})", "",
             "| schedule | case | status | attempts | cost | detail |",
             "|---|---|---|---|---|---|"]
    for r in results:
        detail = (r.get("detail") or "").replace("|", "\\|")[:120]
        lines.append(f"| {r['schedule']} | `{r['case']}` | **{r['status']}** | "
                     f"{r.get('attempts') or ''} | ${r.get('cost_usd') or 0:.4f} | {detail} |")
    if not results:
        lines.append("| (nothing due) | | | | | |")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dataset", choices=DATASETS, default="hospital")
    ap.add_argument("--schedules", default=SCHEDULES_PATH)
    ap.add_argument("--all", action="store_true", help="ignore cadence; run every schedule")
    ap.add_argument("--provider", default=os.environ.get("PLANNER_PROVIDER"))
    ap.add_argument("--min-interval", type=float, default=None)
    ap.add_argument("--summary", help="write the results as JSON here")
    ap.add_argument("--file-issues", metavar="SUMMARY",
                    help="open/update/close GitHub issues from a summary, then exit")
    args = ap.parse_args()

    if args.file_issues:
        file_issues(args.file_issues)
        return EXIT_OK

    config = load_schedules(args.schedules)
    if args.min_interval is not None:
        os.environ["PLANNER_MIN_INTERVAL_SEC"] = str(args.min_interval)
    # eval_harness binds the bank and PLANNER_SCHEMA to EVAL_DATASET at import,
    # so set it first; this is also why one process runs one dataset.
    os.environ["EVAL_DATASET"] = args.dataset
    import eval_harness as eh
    from agent_graph import GovernedAgentGraph
    from data_backends import LocalDuckDBBackend

    if not os.path.exists(eh.GOLD_DB):
        print(f"missing {eh.GOLD_DB} -- build the {args.dataset} Gold first")
        return EXIT_FAILED
    cases = {c["id"]: dict(c) for c in eh.CASES}
    ref = LocalDuckDBBackend(eh.GOLD_DB)
    for s in config["schedules"]:
        if s["dataset"] == args.dataset:
            _, cases[s["case"]]["_ref_rows"] = ref.execute(cases[s["case"]]["reference_sql"])

    agent = GovernedAgentGraph(db_path=eh.GOLD_DB, provider=args.provider)
    store = ResultStore()
    results = run_schedules(config, cases, agent, store, eh._is_provider_error,
                            run_all=args.all, dataset=args.dataset)
    code = exit_code(results)

    for r in results:
        print(f"[{r['status']:>14}] {r['schedule']:32} {(r.get('detail') or '')[:70]}")
    if not results:
        print("nothing due")
    print(f"exit {code}")
    if args.summary:
        with open(args.summary, "w", encoding="utf-8") as f:
            public = [r if r["dataset"] in DETAIL_VALUES_OK else
                      {k: v for k, v in r.items() if k not in ("result_hash", "prev_hash")}
                      for r in results]
            json.dump({"exit_code": code, "dataset": args.dataset,
                       "planner": f"{agent.planner.provider} ({agent.planner._model})",
                       "results": public}, f, indent=2)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(_markdown(results, code))
    return code


if __name__ == "__main__":
    sys.exit(main())
