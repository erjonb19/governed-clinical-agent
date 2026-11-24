"""audit_store.py -- the audit trail in an append-only table (backlog item 39).

Before this, the audit log was JSONL under logs/ on Render's disk: lost on
every redeploy, editable by the app, never backed up. With STATE_DB_URL set
(the item-19 Postgres), every audit event is ALSO written to `audit_events`:

* append-only: a trigger refuses every UPDATE, DELETE and TRUNCATE on the
  table, whoever runs it (in Postgres and in SQLite);
* tamper-evident: each row carries `hash = sha256(prev_hash + its content)`,
  so editing, removing or reordering a row (for instance after someone with
  DDL rights disables the trigger) breaks the chain, and `verify()` names the
  first bad row;
* off the request path: AuditLogger hands events to one background writer per
  process (a bounded queue), so a slow or sleeping database never slows a
  request. A failed or dropped write is counted by `record_failure`
  (item 63) and shows on /metrics; the JSONL still has the event.

    python audit_store.py verify      # checks the chain on STATE_DB_URL

AUDIT_DB=0 turns the table off (the test suite does, except in its own tests).
Without STATE_DB_URL nothing is written here and the JSONL is the only record,
as before. Restore steps and a dated restore test: docs/runbooks/audit-restore.md.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import queue
import sys
import threading
from typing import Any, Optional

GENESIS = "0" * 64
_COLUMNS = ("event_id", "ts", "event_type", "decision", "capability", "principal", "payload")

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


def row_from_event(event: dict[str, Any]) -> dict[str, Any]:
    """An AuditEvent.to_dict() (already redacted) as a table row."""
    return {
        "event_id": str(event.get("event_id") or ""),
        "ts": str(event.get("timestamp") or ""),
        "event_type": str(event.get("event_type") or ""),
        "decision": str(event.get("decision") or ""),
        "capability": str(event.get("capability") or ""),
        "principal": event.get("principal"),
        "payload": json.dumps(event, sort_keys=True, default=str),
    }


class AuditStore:
    """The table. `sqlite_path` is used when STATE_DB_URL is not Postgres
    (tests); on the deploy the store is always STATE_DB_URL."""

    def __init__(self, sqlite_path: str = "logs/audit_events.sqlite"):
        self.path = sqlite_path
        self._ready = False

    def _ensure(self, con: Any) -> None:
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
            con.execute("DROP TRIGGER IF EXISTS audit_events_no_change ON audit_events")
            con.execute(
                "CREATE TRIGGER audit_events_no_change BEFORE UPDATE OR DELETE ON audit_events "
                "FOR EACH ROW EXECUTE FUNCTION audit_events_append_only()"
            )
            con.execute("DROP TRIGGER IF EXISTS audit_events_no_truncate ON audit_events")
            con.execute(
                "CREATE TRIGGER audit_events_no_truncate BEFORE TRUNCATE ON audit_events "
                "FOR EACH STATEMENT EXECUTE FUNCTION audit_events_append_only()"
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
        """Append events (AuditEvent dicts) in one transaction, chained."""
        if not events:
            return 0
        try:
            return self._append(events)
        except Exception:
            # the table may have been dropped under us (tests) or the
            # connection lost: rebuild the DDL once, then give up loudly
            self._ready = False
            return self._append(events)

    def _append(self, events: list[dict[str, Any]]) -> int:
        import state_db

        with state_db.connect(self.path) as con:
            self._ensure(con)
            if con.dialect == "postgres":
                # one writer at a time per database: the chain has one order
                con.execute("SELECT pg_advisory_xact_lock(390039)")
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

    def verify(self) -> dict[str, Any]:
        """Walk the chain: {'ok', 'rows', 'first_bad_seq'}."""
        import state_db

        with state_db.connect(self.path) as con:
            self._ensure(con)
            rows = con.execute(
                "SELECT seq, event_id, ts, event_type, decision, capability, principal, "
                "payload, prev_hash, hash FROM audit_events ORDER BY seq"
            ).fetchall()
        prev = GENESIS
        for r in rows:
            r = dict(r)
            if r["prev_hash"] != prev or chain_hash(prev, r) != r["hash"]:
                return {"ok": False, "rows": len(rows), "first_bad_seq": r["seq"]}
            prev = r["hash"]
        return {"ok": True, "rows": len(rows), "first_bad_seq": None}


class Writer:
    """One background thread per process that drains a bounded queue into the
    store. Never blocks or raises into the caller."""

    def __init__(self, store: AuditStore, maxsize: int = 10_000):
        self.store = store
        self._q: "queue.Queue[dict[str, Any]]" = queue.Queue(maxsize=maxsize)
        self._idle = threading.Event()
        self._idle.set()
        self._t = threading.Thread(target=self._run, name="audit-store", daemon=True)
        self._t.start()
        atexit.register(self.flush, 5.0)

    def put(self, event: dict[str, Any]) -> None:
        try:
            self._idle.clear()
            self._q.put_nowait(event)
        except queue.Full as e:
            from src.runtime.audit_logger import record_failure

            record_failure("audit_store_queue_full", e)

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
                from src.runtime.audit_logger import record_failure

                record_failure("audit_store", e)
            finally:
                for _ in batch:
                    self._q.task_done()
                if self._q.empty():
                    self._idle.set()

    def flush(self, timeout: float = 10.0) -> bool:
        """Wait until everything queued so far is written (or failed)."""
        return self._idle.wait(timeout)


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
    if argv[1:] != ["verify"]:
        print("usage: python audit_store.py verify", file=sys.stderr)
        return 2
    import state_db

    if not state_db.is_postgres():
        print("STATE_DB_URL is not a Postgres URL: no audit table to verify", file=sys.stderr)
        return 2
    out = AuditStore().verify()
    print(json.dumps(out))
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
