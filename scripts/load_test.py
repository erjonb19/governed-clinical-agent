"""Load test for /query on RECORDED model answers (backlog item 20).

Starts the real API (`uvicorn app:app`) on the committed hospital Gold with
PLANNER_PROVIDER=replay, so every plan and critic call is answered from
tests/e2e/recordings/llm.json: no model, no tokens, $0, and the numbers measure
OUR stack (graph, guard, DuckDB, tracing, the lock) rather than a provider.
Then N concurrent clients each send the recorded question; the script reports
p50 / p95 / p99 latency, throughput and errors per concurrency level.

    python scripts/load_test.py                       # 1, 5 and 20 users
    python scripts/load_test.py --users 1 5 --requests 40 --json out.json

The rate limiter is raised for the run (it would turn the test into a 429
count), and the answer cache (llm_cache) is off, so every request runs the
graph. Scale claims are only ever "p95 X ms at N users on <machine>, recorded
answers" (BACKLOG rules for 15-27).
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import socket
import statistics
import subprocess
import sys
import threading
import time
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
QUESTION = "How many hospitals have an overall star rating of exactly 5?"  # recorded in llm.json
KEY = "load-test-key"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _post(url: str, body: dict, timeout: float = 60) -> tuple[int, float]:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json", "X-API-Key": KEY},
    )
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            ok = json.loads(r.read()).get("allowed") is True
            return (200 if ok else 299), (time.perf_counter() - t0) * 1000
    except urllib.error.HTTPError as e:
        return e.code, (time.perf_counter() - t0) * 1000
    except Exception:
        return 0, (time.perf_counter() - t0) * 1000


def _pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    k = max(0, min(len(xs) - 1, int(round(p / 100 * len(xs) + 0.5)) - 1))
    return xs[k]


def run_level(base: str, users: int, per_user: int) -> dict:
    lat: list[float] = []
    codes: list[int] = []
    lock = threading.Lock()

    def worker() -> None:
        for _ in range(per_user):
            code, ms = _post(base + "/v1/query", {"question": QUESTION})
            with lock:
                lat.append(ms)
                codes.append(code)

    t0 = time.perf_counter()
    threads = [threading.Thread(target=worker) for _ in range(users)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.perf_counter() - t0
    good = [ms for ms, c in zip(lat, codes) if c == 200]
    return {
        "users": users,
        "requests": len(codes),
        "ok": len(good),
        "errors": {str(c): codes.count(c) for c in sorted(set(codes)) if c != 200},
        "p50_ms": round(statistics.median(good), 1) if good else None,
        "p95_ms": round(_pct(good, 95), 1) if good else None,
        "p99_ms": round(_pct(good, 99), 1) if good else None,
        "throughput_rps": round(len(good) / wall, 2) if wall else None,
        "wall_s": round(wall, 2),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--users", type=int, nargs="+", default=[1, 5, 20])
    ap.add_argument("--requests", type=int, default=60, help="requests per level")
    ap.add_argument("--json", help="write the results here")
    args = ap.parse_args()

    port = _free_port()
    import shutil
    import tempfile

    tmp = tempfile.mkdtemp(prefix="load_test_")
    env = {
        **os.environ,
        "PLANNER_PROVIDER": "replay",
        "ALLOW_REPLAY": "1",
        "API_KEY": KEY,
        "QUERY_MODE": "graph",
        "LLM_CACHE": "0",
        "PLANNER_FALLBACK": "none",
        "RATE_LIMIT_QUERY": "100000/minute",
        "RATE_LIMIT_READ": "100000/minute",
        "TRACE_DB": os.path.join(tmp, "traces.sqlite"),
        "CHECKPOINT_DB": os.path.join(tmp, "cp.sqlite"),
        "APPROVALS_DB": os.path.join(tmp, "approvals.sqlite"),
        "AUDIT_LOG_DIR": os.path.join(tmp, "audit"),
        "PLANNER_METRICS_LOG": os.path.join(tmp, "metrics.jsonl"),
    }
    env.pop("STATE_DB_URL", None)
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--port", str(port), "--log-level", "warning"],
        cwd=ROOT,
        env=env,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(120):
            try:
                urllib.request.urlopen(base + "/health", timeout=2)
                break
            except Exception:
                time.sleep(0.5)
        code, _ = _post(base + "/v1/query", {"question": QUESTION})  # warm-up
        if code != 200:
            print(f"warm-up failed ({code}): is the question still recorded?", file=sys.stderr)
            return 1
        results = []
        for u in args.users:
            per_user = max(1, args.requests // u)
            r = run_level(base, u, per_user)
            results.append(r)
            print(
                f"{u:>3} users: {r['ok']}/{r['requests']} ok  p50 {r['p50_ms']} ms  "
                f"p95 {r['p95_ms']} ms  p99 {r['p99_ms']} ms  {r['throughput_rps']} req/s  "
                f"errors {r['errors']}"
            )
        out = {
            "machine": f"{platform.platform()} / {os.cpu_count()} CPUs / Python "
            f"{platform.python_version()}",
            "mode": "QUERY_MODE=graph, PLANNER_PROVIDER=replay, LLM_CACHE=0",
            "question": QUESTION,
            "results": results,
        }
        if args.json:
            with open(args.json, "w") as f:
                json.dump(out, f, indent=2)
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
