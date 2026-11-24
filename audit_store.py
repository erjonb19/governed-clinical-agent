"""audit_store.py -- the audit trail in an append-only table (backlog item 39).

Before this, the audit log was JSONL under logs/ on Render's disk: lost on
every redeploy, editable by the app, never backed up. With STATE_DB_URL set
(the item-19 Postgres), every audit event is ALSO written to `audit_events`:

* append-only: triggers refuse every UPDATE, DELETE and TRUNCATE on the table
  for anyone who has not first switched them off -- which the table's owner can
  (that is the app's own STATE_DB_URL role: anyone holding that connection
  string can disable the triggers; see the runbook's limits);
* tamper-evident: each row carries `hash = sha256(prev_hash + its content)`, so
  editing, removing from the middle or reordering rows breaks the chain, and
  `verify()` names the first bad row. Removing the NEWEST rows leaves a valid
  shorter chain: that is caught only against an anchor kept elsewhere -- note
  `last_hash` from `verify` (or a dated dump) and later run
  `verify --anchor <hash>`, which fails if that row is gone;
* off the request path: AuditLogger hands events to one background writer per
  process (a bounded queue), so a slow or sleeping database never slows a
  request. Every event that does not reach the table -- a failed batch, a full
  queue, a queue still holding events at exit -- is counted, one per event, by
  `record_failure` (item 63, on /metrics); the JSONL still has it.

A batch whose COMMIT outcome is unknown (the connection dropped as it
committed) is written again, so an event can appear twice: readers dedupe on
`event_id`. A duplicate is better than a loss for an audit trail. The table
never holds query results: `rows` are dropped here whatever logger sent them.

    python audit_store.py verify [--anchor <hash>]   # read-only check on STATE_DB_URL

AUDIT_DB=0 turns the table off (the test suite does, except in its own tests).
Without STATE_DB_URL nothing is written here and the JSONL is the only record,
as before. The SQLite form exists for tests only; it has no cross-process lock.
Restore steps and a dated restore test: docs/runbooks/audit-restore.md.
"""

from __future__ import annotations

import atexit
import copy
import hashlib
import json
import os
import queue
import sys
import threading
from typing import Any, Optional

GENESIS = "0" * 64
_COLUMNS = ("event_id", "ts", "event_type", "decision", "capability", "principal", "payload")
_LOCK_ID = 390039  # pg_advisory_xact_lock key: one writer (and one DDL run) at a time

_PG_GUARD = """
CREATE OR REPLACE FUNCTION audit_events_append_only() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'audit_events is append-only';
END
$$ LANGUAGE plpgsql
"""


def _canonical(row: dict[str, Any]) -> str:
    return json.dumps({k: row.get(k) for k in _COLUMNS}, sort_keys=True, separators=(",", ":"))


def chain_hash(prev: str, row: dict[str, Any]) -> str:
    return hashlib.sha256((prev + _canonical(row)).encode("utf-8")).hexdigest()


def _text(v: Any) -> str:
    # Postgres TEXT refuses NUL: one would fail the whole batch
    return str(v).replace("\x00", "")


def _without_rows(event: dict[str, Any]) -> dict[str, Any]:
    out = event.get("execution_result")
    inner = out.get("output") if isinstance(out, dict) else None
    if isinstance(inner, dict) and "rows" in inner:
        event = copy.deepcopy(event)
        event["execution_result"]["output"].pop("rows", None)
    return event


def row_from_event(event: dict[str, Any]) -> dict[str, Any]:
    """An AuditEvent.to_dict() (already redacted) as a table row, without rows."""
    event = _without_rows(event)
    principal = event.get("principal")
    return {
        "event_id": _text(event.get("event_id") or ""),
        "ts": _text(event.get("timestamp") or ""),
        "event_type": _text(event.get("event_type") or ""),
        "decision": _text(event.get("decision") or ""),
        "capability": _text(event.get("capability") or ""),
        "principal": None if principal is None else _text(principal),
        "payload": json.dumps(event, sort_keys=True, default=str),
    }


class AuditStore:
    """The table. `sqlite_path` is used when STATE_DB_URL is not Postgres
    (tests); on the deploy the store is always STATE_DB_URL."""

    def __init__(self, sqlite_path: str = "logs/audit_events.sqlite"):
        self.path = sqlite_path
        self._ready = False

    def _ensure(self, con: Any) -> None:
        """Create the table and its guards once. On Postgres the caller holds
        the advisory lock first, so two processes starting together (Render
        overlaps instances on a deploy; the MCP server shares the database)
        never run this DDL at the same moment."""
        if self._ready:
            return
        import state_db

        con.execute(
            "CREATE TABLE IF NOT EXISTS audit_events ("
            f"seq {state_db.autoincrement_pk(con)}, "
            "event_id TEXT NOT NULL, ts TEXT NOT NULL, event_type TEXT NOT NULL, "
            "decision TEXT NOT NULL, capability TEXT NOT NULL, principal TEXT, "
            "payload TEXT NOT NULL, prev_hash TEXT NOT NULL, hash TEXT NOT NULL)"
        )
        if con.dialect == "postgres":
            con.execute(_PG_GUARD)
            con.execute(
                "CREATE OR REPLACE TRIGGER audit_events_no_change BEFORE UPDATE OR DELETE "
                "ON audit_events FOR EACH ROW EXECUTE FUNCTION audit_events_append_only()"
            )
            con.execute(
                "CREATE OR REPLACE TRIGGER audit_events_no_truncate BEFORE TRUNCATE "
                "ON audit_events FOR EACH STATEMENT EXECUTE FUNCTION audit_events_append_only()"
            )
        else:
            for op in ("UPDATE", "DELETE"):
                con.execute(
                    f"CREATE TRIGGER IF NOT EXISTS audit_events_no_{op.lower()} "
                    f"BEFORE {op} ON audit_events "
                    "BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END"
                )
        self._ready = True

    def append(self, events: list[dict[str, Any]]) -> int:
        """Append events (AuditEvent dicts) in one transaction, chained. One
        retry, after re-creating the DDL (the table may have been dropped
        under us, or the connection lost); see the module note on duplicates."""
        if not events:
            return 0
        try:
            return self._append(events)
        except Exception:
            self._ready = False
            return self._append(events)

    def _append(self, events: list[dict[str, Any]]) -> int:
        import state_db

        with state_db.connect(self.path) as con:
            if con.dialect == "postgres":
                # never wait forever on a writer whose connection hangs
                con.execute("SET LOCAL lock_timeout = '10s'")
                con.execute(f"SELECT pg_advisory_xact_lock({_LOCK_ID})")
            self._ensure(con)
            last = con.execute("SELECT hash FROM audit_events ORDER BY seq DESC LIMIT 1").fetchone()
            prev = last["hash"] if last else GENESIS
            for ev in events:
                row = row_from_event(ev)
                h = chain_hash(prev, row)
                con.execute(
                    "INSERT INTO audit_events (event_id, ts, event_type, decision, capability, "
                    "principal, payload, prev_hash, hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (*(row[c] for c in _COLUMNS), prev, h),
                )
                prev = h
        return len(events)

    def verify(self, anchor: Optional[str] = None) -> dict[str, Any]:
        """Walk the chain, read-only (no DDL, no locks beyond a read):
        {'ok', 'rows', 'first_bad_seq', 'last_hash', 'anchor_found'}."""
        import state_db

        with state_db.connect(self.path) as con:
            try:
                rows = con.execute(
                    "SELECT seq, event_id, ts, event_type, decision, capability, principal, "
                    "payload, prev_hash, hash FROM audit_events ORDER BY seq"
                ).fetchall()
            except Exception:
                rows = None  # no table yet
        out: dict[str, Any] = {"ok": True, "rows": 0, "first_bad_seq": None, "last_hash": None}
        if anchor is not None:
            out["anchor_found"] = False
        prev = GENESIS
        for r in rows or []:
            r = dict(r)
            out["rows"] += 1
            if out["ok"] and (r["prev_hash"] != prev or chain_hash(prev, r) != r["hash"]):
                out["ok"], out["first_bad_seq"] = False, r["seq"]
            if anchor is not None and r["hash"] == anchor:
                out["anchor_found"] = True
            prev = r["hash"]
        out["last_hash"] = prev if rows else None
        if anchor is not None and not out["anchor_found"]:
            out["ok"] = False  # the anchored row (or everything after it) is gone
        return out


class Writer:
    """One background thread per process that drains a bounded queue into the
    store. Never blocks or raises into the caller; every event that does not
    reach the store is counted."""

    def __init__(self, store: AuditStore, maxsize: int = 10_000):
        self.store = store
        self._q: "queue.Queue[dict[str, Any]]" = queue.Queue(maxsize=maxsize)
        self._t = threading.Thread(target=self._run, name="audit-store", daemon=True)
        self._t.start()
        atexit.register(self._at_exit)

    @staticmethod
    def _count(where: str, exc: BaseException, events: int) -> None:
        from src.runtime.audit_logger import record_failure

        record_failure(where, exc, events)

    def put(self, event: dict[str, Any]) -> None:
        try:
            self._q.put_nowait(event)
        except queue.Full as e:
            self._count("audit_store_queue_full", e, 1)

    def _run(self) -> None:
        while True:
            batch = [self._q.get()]
            while len(batch) < 100:
                try:
                    batch.append(self._q.get_nowait())
                except queue.Empty:
                    break
            try:
                self.store.append(batch)
            except Exception as e:  # noqa: BLE001 -- counted, never raised
                self._count("audit_store", e, len(batch))
            finally:
                for _ in batch:
                    self._q.task_done()

    def flush(self, timeout: float = 10.0) -> bool:
        """Wait until every event queued so far is written or counted as lost."""
        with self._q.all_tasks_done:
            return self._q.all_tasks_done.wait_for(lambda: self._q.unfinished_tasks == 0, timeout)

    def _at_exit(self) -> None:
        if not self.flush(5.0):
            left = self._q.unfinished_tasks
            self._count("audit_store_shutdown", TimeoutError("unwritten at exit"), left)


_WRITER: Optional[Writer] = None
_WRITER_LOCK = threading.Lock()


def enabled() -> bool:
    import state_db

    return os.environ.get("AUDIT_DB", "auto").strip() != "0" and state_db.is_postgres()


def writer() -> Optional[Writer]:
    """The process's writer when the table is on, else None."""
    global _WRITER
    if not enabled():
        return None
    with _WRITER_LOCK:
        if _WRITER is None:
            _WRITER = Writer(AuditStore())
        return _WRITER


def main(argv: list[str]) -> int:
    args = argv[1:]
    anchor = None
    if args[:1] == ["verify"] and len(args) == 3 and args[1] == "--anchor":
        anchor = args[2]
    elif args != ["verify"]:
        print("usage: python audit_store.py verify [--anchor <hash>]", file=sys.stderr)
        return 2
    import state_db

    if not state_db.is_postgres():
        print("STATE_DB_URL is not a Postgres URL: no audit table to verify", file=sys.stderr)
        return 2
    out = AuditStore().verify(anchor)
    print(json.dumps(out))
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
