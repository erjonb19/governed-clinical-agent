"""Backlog item 39: the audit trail in an append-only, hash-chained table.

Runs on SQLite (a file under tmp_path) and, in the pytest-postgres CI job
(TEST_STATE_DB_URL), on Postgres, where the same tests hit the real triggers.
"""

import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import audit_store  # noqa: E402
import state_db  # noqa: E402
from src.runtime import audit_logger as al  # noqa: E402


def _event(i, **kw):
    return {
        "event_id": f"e{i}",
        "timestamp": f"2026-11-24T00:00:{i:02d}",
        "event_type": "tool_execution",
        "decision": "allow",
        "capability": "analytics.query_aggregate",
        "principal": "key:abcd",
        "reason": "ok",
        **kw,
    }


@pytest.fixture
def store(tmp_path):
    return audit_store.AuditStore(str(tmp_path / "audit.sqlite"))


def _raw(store, sql, params=()):
    with state_db.connect(store.path) as con:
        return con.execute(sql, params)


def test_events_are_chained_and_verify(store):
    assert store.append([_event(1), _event(2)]) == 2
    store.append([_event(3)])
    out = store.verify()
    assert out["ok"] and out["rows"] == 3 and out["first_bad_seq"] is None
    with state_db.connect(store.path) as con:
        rows = con.execute("SELECT prev_hash, hash FROM audit_events ORDER BY seq").fetchall()
    assert rows[0]["prev_hash"] == audit_store.GENESIS
    assert rows[1]["prev_hash"] == rows[0]["hash"] and rows[2]["prev_hash"] == rows[1]["hash"]


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE audit_events SET decision = 'deny'",
        "DELETE FROM audit_events",
    ],
)
def test_rows_cannot_be_changed_or_removed(store, sql):
    store.append([_event(1)])
    with pytest.raises(Exception) as e:
        _raw(store, sql)
    assert "append-only" in str(e.value)
    assert store.verify()["rows"] == 1


@pytest.mark.skipif(
    not state_db.is_postgres(os.environ.get("TEST_STATE_DB_URL", "")),
    reason="TRUNCATE is Postgres's; SQLite has none",
)
def test_truncate_is_refused_on_postgres(store):
    store.append([_event(1)])
    with pytest.raises(Exception) as e:
        _raw(store, "TRUNCATE audit_events")
    assert "append-only" in str(e.value)


def test_an_edit_behind_the_triggers_back_breaks_the_chain(store):
    store.append([_event(1), _event(2), _event(3)])
    # someone with DDL rights takes the guard off and rewrites history
    if state_db.is_postgres():
        _raw(store, "ALTER TABLE audit_events DISABLE TRIGGER USER")
    else:
        _raw(store, "DROP TRIGGER audit_events_no_update")
    _raw(store, "UPDATE audit_events SET decision = 'deny' WHERE event_id = 'e2'")
    out = store.verify()
    assert out["ok"] is False and out["first_bad_seq"] is not None
    with state_db.connect(store.path) as con:
        seq2 = con.execute("SELECT seq FROM audit_events WHERE event_id = 'e2'").fetchone()["seq"]
    assert out["first_bad_seq"] == seq2


def test_a_removed_row_breaks_the_chain(store):
    store.append([_event(1), _event(2), _event(3)])
    if state_db.is_postgres():
        _raw(store, "ALTER TABLE audit_events DISABLE TRIGGER USER")
    else:
        _raw(store, "DROP TRIGGER audit_events_no_delete")
    _raw(store, "DELETE FROM audit_events WHERE event_id = 'e2'")
    assert store.verify()["ok"] is False


def test_the_logger_hands_every_event_to_the_table(tmp_path, store):
    w = audit_store.Writer(store)
    log = al.AuditLogger(
        log_dir=tmp_path / "audit", agent_id="t", max_buffer_size=1, enable_console=False
    )
    log._store_writer = w
    log.log_auth_failure(
        client="203.0.113.7", path="/query", surface="api", failures_in_window=1, throttled=False
    )
    log.log_answer(
        capability="analytics.query_aggregate",
        allowed=True,
        decided_by="guard",
        version={"prompt_version": "p-test"},
        trace_id="t1",
    )
    assert w.flush(10)
    jsonl = [p for p in (tmp_path / "audit").iterdir()]
    lines = sum(len(p.read_text().splitlines()) for p in jsonl)
    out = store.verify()
    assert out["ok"] and out["rows"] == lines == 2


def test_a_failing_store_is_counted_never_raised(tmp_path):
    class Down(audit_store.AuditStore):
        def append(self, events):
            raise OSError("connection refused")

    al.reset_failures()
    w = audit_store.Writer(Down(str(tmp_path / "x.sqlite")))
    w.put(_event(1))
    assert w.flush(10)
    assert al.failure_count() >= 1


def test_off_without_postgres_or_with_audit_db_0(monkeypatch):
    monkeypatch.setenv("AUDIT_DB", "0")
    assert audit_store.writer() is None
    monkeypatch.setenv("AUDIT_DB", "auto")
    monkeypatch.delenv("STATE_DB_URL", raising=False)
    assert audit_store.writer() is None


def test_result_rows_never_reach_the_table_whoever_logs(store):
    # a plain AuditLogger (a demo, an eval script) does not drop rows itself
    ev = _event(1, execution_result={"output": {"row_count": 1, "rows": [{"n": 1180}]}})
    store.append([ev])
    with state_db.connect(store.path) as con:
        payload = con.execute("SELECT payload FROM audit_events").fetchone()["payload"]
    assert '"row_count": 1' in payload and '"rows"' not in payload and "1180" not in payload
    assert "rows" in ev["execution_result"]["output"]  # the caller's dict is not changed


def test_deleting_the_newest_rows_is_caught_by_an_anchor(store):
    store.append([_event(1), _event(2), _event(3)])
    anchor = store.verify()["last_hash"]
    if state_db.is_postgres():
        _raw(store, "ALTER TABLE audit_events DISABLE TRIGGER USER")
    else:
        _raw(store, "DROP TRIGGER audit_events_no_delete")
    _raw(store, "DELETE FROM audit_events WHERE event_id = 'e3'")
    assert store.verify()["ok"] is True  # the shorter chain is still a valid chain...
    out = store.verify(anchor)
    assert out["ok"] is False and out["anchor_found"] is False  # ...the anchor is not


def test_verify_is_read_only_and_copes_with_no_table(tmp_path):
    fresh = audit_store.AuditStore(str(tmp_path / "empty.sqlite"))
    out = fresh.verify()
    assert out == {"ok": True, "rows": 0, "first_bad_seq": None, "last_hash": None}
    if not state_db.is_postgres():
        assert not (tmp_path / "empty.sqlite").exists() or _tables(fresh) == set()


def _tables(store):
    with state_db.connect(store.path) as con:
        return {r["name"] for r in con.execute("SELECT name FROM sqlite_master").fetchall()}


def test_a_non_string_principal_or_a_nul_cannot_break_the_chain(store):
    store.append([_event(1, principal=5), _event(2, capability="a\x00b")])
    assert store.verify()["ok"] is True


def test_a_lost_batch_counts_each_event(tmp_path):
    class Down(audit_store.AuditStore):
        def append(self, events):
            raise OSError("down")

    al.reset_failures()
    w = audit_store.Writer(Down(str(tmp_path / "x.sqlite")))
    for i in range(5):
        w.put(_event(i))
    assert w.flush(10)
    assert al.failure_count() == 5


def _writer_process(url, n, start):
    import os

    os.environ["STATE_DB_URL"] = url
    start.wait()
    audit_store.AuditStore().append([_event(i, event_id=f"p{os.getpid()}-{i}") for i in range(n)])


@pytest.mark.skipif(
    not state_db.is_postgres(os.environ.get("TEST_STATE_DB_URL", "")),
    reason="cross-process DDL race is Postgres's",
)
def test_processes_starting_together_lose_nothing(monkeypatch):
    import multiprocessing as mp

    url = os.environ["TEST_STATE_DB_URL"]
    ctx = mp.get_context("spawn")
    start = ctx.Event()
    procs = [ctx.Process(target=_writer_process, args=(url, 30, start)) for _ in range(4)]
    for p in procs:
        p.start()
    start.set()
    for p in procs:
        p.join(60)
    assert all(p.exitcode == 0 for p in procs)
    out = audit_store.AuditStore().verify()
    assert out["ok"] and out["rows"] == 120


def test_verify_cli_needs_postgres(monkeypatch, capsys):
    monkeypatch.delenv("STATE_DB_URL", raising=False)
    assert audit_store.main(["audit_store.py", "verify"]) == 2
    assert audit_store.main(["audit_store.py"]) == 2
