"""
cost_tracker.py
===============
Aggregate cost/latency tracking for planner calls. Per-call metrics answer
"what did THIS request cost"; this answers "what are we spending over time and
where is the latency" -- which is what "optimize LLM cost" actually means.

Design: append one JSON line per call to a JSONL file (same pattern as the
audit log), then summarize on demand. File-based and dependency-free; the same
data can feed the AIOps panel.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timezone

METRICS_LOG = os.environ.get("PLANNER_METRICS_LOG", "logs/planner_metrics.jsonl")
_LOCK = threading.Lock()


def _int_env(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, default)))
    except ValueError:
        return default


# Backlog item 60: the log is capped. Past METRICS_LOG_MAX_BYTES it is renamed
# to `<file>.1` (the old `.1` to `.2`, ...), keeping METRICS_LOG_KEEP old files;
# summaries read the current file only.
MAX_BYTES = _int_env("METRICS_LOG_MAX_BYTES", 5_000_000)
KEEP = _int_env("METRICS_LOG_KEEP", 2)


def rotate(path: str, max_bytes: int, keep: int) -> None:
    """Rename `path` to `path.1` (shifting older ones) once it passes max_bytes;
    the oldest beyond `keep` is removed. 0 for max_bytes turns this off."""
    if not max_bytes or not os.path.exists(path) or os.path.getsize(path) < max_bytes:
        return
    if keep <= 0:
        os.remove(path)
        return
    oldest = f"{path}.{keep}"
    if os.path.exists(oldest):
        os.remove(oldest)
    for i in range(keep - 1, 0, -1):
        if os.path.exists(f"{path}.{i}"):
            os.replace(f"{path}.{i}", f"{path}.{i + 1}")
    os.replace(path, f"{path}.1")


def record(metrics: dict, question: str | None = None) -> None:
    """Append one planner call's metrics. Best-effort; never raises to caller."""
    try:
        os.makedirs(os.path.dirname(METRICS_LOG) or ".", exist_ok=True)
        row = dict(metrics)
        row["ts"] = datetime.now(timezone.utc).isoformat()
        if question:
            # item 60: a hash and a length, never the text -- the question can
            # hold anything a visitor typed, and this file is kept
            row["question_sha256"] = hashlib.sha256(question.encode()).hexdigest()[:16]
            row["question_chars"] = len(question)
        with _LOCK:
            rotate(METRICS_LOG, MAX_BYTES, KEEP)
            with open(METRICS_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
    except Exception:
        pass  # metrics are best-effort, never break a request


def summarize(path: str | None = None) -> dict:
    """Aggregate the metrics log into totals and averages."""
    path = path or METRICS_LOG
    if not os.path.exists(path):
        return {"calls": 0}
    n = 0
    tot_tokens = tot_prompt = tot_completion = 0
    tot_cost = 0.0
    latencies = []
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        n += 1
        tot_prompt += r.get("prompt_tokens", 0)
        tot_completion += r.get("completion_tokens", 0)
        tot_tokens += r.get("total_tokens", 0)
        tot_cost += r.get("est_cost_usd", 0.0)
        if r.get("latency_ms") is not None:
            latencies.append(r["latency_ms"])
    latencies.sort()

    def pct(p):
        if not latencies:
            return None
        i = min(len(latencies) - 1, int(p / 100 * len(latencies)))
        return latencies[i]

    return {
        "calls": n,
        "total_tokens": tot_tokens,
        "total_prompt_tokens": tot_prompt,
        "total_completion_tokens": tot_completion,
        "total_est_cost_usd": round(tot_cost, 6),
        "avg_tokens_per_call": round(tot_tokens / n, 1) if n else 0,
        "avg_cost_per_call_usd": round(tot_cost / n, 6) if n else 0,
        "latency_ms_avg": round(sum(latencies) / len(latencies)) if latencies else None,
        "latency_ms_p50": pct(50),
        "latency_ms_p95": pct(95),
        "latency_ms_p99": pct(99),
    }


if __name__ == "__main__":
    import sys

    print(json.dumps(summarize(sys.argv[1] if len(sys.argv) > 1 else None), indent=2))
