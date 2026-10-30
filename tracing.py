"""Run tracing: one trace per run, one span per step, in local SQLite.

WHY THIS FILE EXISTS
Until now a run left nothing behind. `/query` wrote one cost line to
`logs/planner_metrics.jsonl`; the graphs carried a `trajectory` list in memory and
returned it to the caller; nothing was persisted or queryable afterwards. A question
answered an hour ago could not be looked up, and a run that failed between `plan` and
`execute` could not be explained.

This module records every run as a **trace** with **spans** (OpenTelemetry-shaped field
names, so an OTLP exporter can be added later without a schema change):

    traces: trace_id, name, start_ts, end_ts, latency_ms, status, attrs
    spans:  span_id, trace_id, parent_id, name, start_ts, end_ts, latency_ms, status, attrs

Span names used by the graphs and the API: `run`, `plan`, `guard`, `execute`, `retry`,
`approval.propose`, `approval.decision`, `answer`.

HARD RULES THIS FILE KEEPS
- PHI (CLAUDE.md rule 3): spans store the question, the SQL, its hash and row COUNTS.
  They never store result rows. `_scrub()` drops `rows` (and `result`) from every attrs
  dict before it is written, however deep, so a FHIR answer cannot leak into the trace
  store. A unit test asserts no `rows` key is ever written.
- Best effort (same contract as `cost_tracker.record`): a tracer never raises into a
  request. A missing directory, a locked file or a bad path means the run goes
  unrecorded, not that it fails.

The trace id doubles as the LangGraph `thread_id` (PLAN v2 §2 and the operations
track), so one id names the checkpoint thread, the trace and the response.
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from typing import Any, Iterator, Optional

TRACE_DB = os.environ.get("TRACE_DB", "logs/traces.sqlite")
TRACE_MAX_RUNS = int(os.environ.get("TRACE_MAX_RUNS", "2000"))

_FORBIDDEN_KEYS = {"rows", "result", "results"}
_ATTR_MAX_CHARS = 4000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS traces (
    trace_id   TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    start_ts   REAL NOT NULL,
    end_ts     REAL,
    latency_ms INTEGER,
    status     TEXT NOT NULL DEFAULT 'running',
    attrs      TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS spans (
    span_id    TEXT PRIMARY KEY,
    trace_id   TEXT NOT NULL,
    parent_id  TEXT,
    name       TEXT NOT NULL,
    start_ts   REAL NOT NULL,
    end_ts     REAL,
    latency_ms INTEGER,
    status     TEXT NOT NULL DEFAULT 'running',
    attrs      TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS spans_trace ON spans (trace_id, start_ts);
CREATE INDEX IF NOT EXISTS traces_start ON traces (start_ts);
"""

_current_trace: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "tracing_current_trace", default=None)
_current_span: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "tracing_current_span", default=None)


_clock_lock = threading.Lock()
_last_start = 0.0


def _start_ts() -> float:
    """A span start time that is strictly increasing within the process.

    Spans are written when they close, so a parent lands after its children.
    Ordering by start_ts puts them back, but on a coarse clock (Windows ticks
    in milliseconds) parent and child can share a timestamp and the rowid
    tie-break then puts the child first.
    """
    global _last_start
    with _clock_lock:
        t = time.time()
        if t <= _last_start:
            t = _last_start + 1e-6
        _last_start = t
        return t


def new_id() -> str:
    return uuid.uuid4().hex[:16]


def sql_sha256(sql: Optional[str]) -> Optional[str]:
    if not sql:
        return None
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()[:16]


def _scrub(value: Any) -> Any:
    """Drop result rows from attrs, however deep, and keep strings bounded."""
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items() if k not in _FORBIDDEN_KEYS}
    if isinstance(value, (list, tuple)):
        return [_scrub(v) for v in value]
    if isinstance(value, str) and len(value) > _ATTR_MAX_CHARS:
        return value[:_ATTR_MAX_CHARS] + "…"
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)


def _dumps(attrs: dict) -> str:
    return json.dumps(_scrub(attrs), default=str, separators=(",", ":"))


class Span:
    """A step inside a trace. Set attributes on it as the step learns things."""

    __slots__ = ("tracer", "trace_id", "span_id", "parent_id", "name", "start_ts",
                 "status", "attrs")

    def __init__(self, tracer: "Tracer", trace_id: str, span_id: str,
                 parent_id: Optional[str], name: str, attrs: dict):
        self.tracer = tracer
        self.trace_id = trace_id
        self.span_id = span_id
        self.parent_id = parent_id
        self.name = name
        self.start_ts = _start_ts()
        self.status = "ok"
        self.attrs = dict(attrs)

    def set(self, **attrs: Any) -> "Span":
        self.attrs.update(attrs)
        return self


class Tracer:
    """Writes traces and spans to SQLite; reads them back for the API."""

    def __init__(self, db_path: Optional[str] = None, max_runs: Optional[int] = None):
        self.db_path = db_path or TRACE_DB
        self.max_runs = max_runs or TRACE_MAX_RUNS
        self._lock = threading.Lock()
        self._ready = False
        self._runs_since_prune = 0

    # ---------------- storage (best effort) ----------------

    def _connect(self) -> Optional[sqlite3.Connection]:
        try:
            d = os.path.dirname(self.db_path)
            if d:
                os.makedirs(d, exist_ok=True)
            con = sqlite3.connect(self.db_path, timeout=2)
            if not self._ready:
                con.executescript(_SCHEMA)
                self._ready = True
            return con
        except Exception:
            return None

    def _write(self, sql: str, params: tuple) -> None:
        with self._lock:
            con = self._connect()
            if con is None:
                return
            try:
                con.execute(sql, params)
                con.commit()
            except Exception:
                pass
            finally:
                con.close()

    def _read(self, sql: str, params: tuple = ()) -> list[tuple]:
        con = self._connect()
        if con is None:
            return []
        try:
            return con.execute(sql, params).fetchall()
        except Exception:
            return []
        finally:
            con.close()

    # ---------------- writing ----------------

    def start_trace(self, name: str = "run", trace_id: Optional[str] = None,
                    **attrs: Any) -> str:
        """Open a trace and make it current. Returns its id (also the thread id)."""
        trace_id = trace_id or new_id()
        self._write(
            "INSERT OR IGNORE INTO traces (trace_id, name, start_ts, status, attrs)"
            " VALUES (?, ?, ?, 'running', ?)",
            (trace_id, name, time.time(), _dumps(attrs)))
        _current_trace.set(trace_id)
        _current_span.set(None)
        return trace_id

    def end_trace(self, trace_id: Optional[str] = None, status: str = "ok",
                  **attrs: Any) -> None:
        trace_id = trace_id or _current_trace.get()
        if not trace_id:
            return
        rows = self._read("SELECT start_ts, attrs FROM traces WHERE trace_id = ?", (trace_id,))
        now = time.time()
        start = rows[0][0] if rows else now
        merged: dict = {}
        if rows:
            try:
                merged = json.loads(rows[0][1])
            except Exception:
                merged = {}
        merged.update(_scrub(attrs))
        self._write(
            "UPDATE traces SET end_ts = ?, latency_ms = ?, status = ?, attrs = ? WHERE trace_id = ?",
            (now, int((now - start) * 1000), status, _dumps(merged), trace_id))
        self._runs_since_prune += 1
        if self._runs_since_prune >= 50:
            self._runs_since_prune = 0
            self.prune()

    @contextlib.contextmanager
    def trace(self, name: str = "run", trace_id: Optional[str] = None,
              **attrs: Any) -> Iterator[str]:
        """A whole run: opens the trace, closes it with the outcome, restores context."""
        prev_t, prev_s = _current_trace.get(), _current_span.get()
        tid = self.start_trace(name, trace_id, **attrs)
        try:
            yield tid
        except BaseException as exc:
            self.end_trace(tid, status="error", error=f"{type(exc).__name__}: {exc}"[:500])
            raise
        else:
            # A body that already closed the trace (e.g. status="error" on a
            # planner failure, then return) keeps its outcome; only close it here
            # if it is still running.
            done = self._read("SELECT status FROM traces WHERE trace_id = ?", (tid,))
            if not done or done[0][0] == "running":
                self.end_trace(tid, status="ok")
        finally:
            _current_trace.set(prev_t)
            _current_span.set(prev_s)

    @contextlib.contextmanager
    def attach(self, trace_id: str) -> Iterator[str]:
        """Append spans to an existing trace (an approval decided hours later)."""
        prev_t, prev_s = _current_trace.get(), _current_span.get()
        _current_trace.set(trace_id)
        _current_span.set(None)
        try:
            yield trace_id
        finally:
            _current_trace.set(prev_t)
            _current_span.set(prev_s)

    @contextlib.contextmanager
    def span(self, name: str, **attrs: Any) -> Iterator[Span]:
        """One step. Nested spans get this one as their parent. Exceptions are
        recorded as status 'error' and re-raised; they are never swallowed."""
        trace_id = _current_trace.get()
        span = Span(self, trace_id or "", new_id(), _current_span.get(), name, attrs)
        token = _current_span.set(span.span_id)
        try:
            yield span
        except BaseException as exc:
            span.status = "error"
            span.attrs.setdefault("error", f"{type(exc).__name__}: {exc}"[:500])
            raise
        finally:
            _current_span.reset(token)
            if trace_id:
                end = time.time()
                self._write(
                    "INSERT OR REPLACE INTO spans (span_id, trace_id, parent_id, name, start_ts,"
                    " end_ts, latency_ms, status, attrs) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (span.span_id, trace_id, span.parent_id, span.name, span.start_ts, end,
                     int((end - span.start_ts) * 1000), span.status, _dumps(span.attrs)))

    def event(self, name: str, **attrs: Any) -> None:
        """A zero-length span: something that happened at a point in time."""
        with self.span(name, **attrs):
            pass

    # ---------------- reading ----------------

    @staticmethod
    def _row(cols: tuple, row: tuple) -> dict:
        d = dict(zip(cols, row))
        try:
            d["attrs"] = json.loads(d.get("attrs") or "{}")
        except Exception:
            d["attrs"] = {}
        return d

    def list_traces(self, limit: int = 50, offset: int = 0) -> list[dict]:
        cols = ("trace_id", "name", "start_ts", "end_ts", "latency_ms", "status", "attrs")
        rows = self._read(
            "SELECT trace_id, name, start_ts, end_ts, latency_ms, status, attrs FROM traces"
            " ORDER BY start_ts DESC LIMIT ? OFFSET ?", (max(1, min(limit, 500)), max(0, offset)))
        out = []
        for r in rows:
            d = self._row(cols, r)
            n = self._read("SELECT COUNT(*) FROM spans WHERE trace_id = ?", (d["trace_id"],))
            d["span_count"] = n[0][0] if n else 0
            out.append(d)
        return out

    def get_trace(self, trace_id: str) -> Optional[dict]:
        tcols = ("trace_id", "name", "start_ts", "end_ts", "latency_ms", "status", "attrs")
        rows = self._read(
            "SELECT trace_id, name, start_ts, end_ts, latency_ms, status, attrs FROM traces"
            " WHERE trace_id = ?", (trace_id,))
        if not rows:
            return None
        trace = self._row(tcols, rows[0])
        scols = ("span_id", "parent_id", "name", "start_ts", "end_ts", "latency_ms", "status", "attrs")
        spans = [self._row(scols, r) for r in self._read(
            "SELECT span_id, parent_id, name, start_ts, end_ts, latency_ms, status, attrs"
            " FROM spans WHERE trace_id = ? ORDER BY start_ts, rowid", (trace_id,))]
        trace["spans"] = spans
        return trace

    def count(self) -> int:
        rows = self._read("SELECT COUNT(*) FROM traces")
        return rows[0][0] if rows else 0

    def prune(self) -> int:
        """Keep the newest `max_runs` traces; delete the rest and their spans."""
        rows = self._read(
            "SELECT trace_id FROM traces ORDER BY start_ts DESC LIMIT -1 OFFSET ?",
            (self.max_runs,))
        if not rows:
            return 0
        with self._lock:
            con = self._connect()
            if con is None:
                return 0
            try:
                ids = [r[0] for r in rows]
                con.executemany("DELETE FROM spans WHERE trace_id = ?", [(i,) for i in ids])
                con.executemany("DELETE FROM traces WHERE trace_id = ?", [(i,) for i in ids])
                con.commit()
                return len(ids)
            except Exception:
                return 0
            finally:
                con.close()


_DEFAULT: Optional[Tracer] = None
_DEFAULT_LOCK = threading.Lock()


def get_tracer() -> Tracer:
    """The process-wide tracer on TRACE_DB. Tests build their own on a tmp path."""
    global _DEFAULT
    with _DEFAULT_LOCK:
        if _DEFAULT is None or _DEFAULT.db_path != TRACE_DB:
            _DEFAULT = Tracer(TRACE_DB)
        return _DEFAULT


def set_tracer(tracer: Optional[Tracer]) -> None:
    global _DEFAULT
    with _DEFAULT_LOCK:
        _DEFAULT = tracer


def current_trace_id() -> Optional[str]:
    return _current_trace.get()
