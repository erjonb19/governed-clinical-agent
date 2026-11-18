"""Backlog item 60: logs that can hold free text are capped, and the approval
queue's decided items expire."""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import cost_tracker  # noqa: E402

QUESTION = "how many visits did Jane Doe of 02134 have?"


def test_the_metrics_log_keeps_a_hash_and_a_length_not_the_question(tmp_path, monkeypatch):
    log = tmp_path / "m.jsonl"
    monkeypatch.setattr(cost_tracker, "METRICS_LOG", str(log))
    cost_tracker.record({"total_tokens": 5}, question=QUESTION)
    row = json.loads(log.read_text())
    assert "question" not in row and "Jane" not in log.read_text()
    assert len(row["question_sha256"]) == 16 and row["question_chars"] == len(QUESTION)


def test_the_metrics_log_rotates_and_keeps_only_so_many(tmp_path, monkeypatch):
    log = tmp_path / "m.jsonl"
    monkeypatch.setattr(cost_tracker, "METRICS_LOG", str(log))
    monkeypatch.setattr(cost_tracker, "MAX_BYTES", 200)
    monkeypatch.setattr(cost_tracker, "KEEP", 2)
    for i in range(40):
        cost_tracker.record({"total_tokens": i, "pad": "x" * 50})
    files = sorted(p.name for p in tmp_path.iterdir())
    assert files == ["m.jsonl", "m.jsonl.1", "m.jsonl.2"], files
    assert all(p.stat().st_size < 400 for p in tmp_path.iterdir())


def test_the_audit_log_rotates_and_keeps_only_so_many(tmp_path, monkeypatch):
    from src.runtime.audit_logger import AuditLogger

    monkeypatch.setenv("AUDIT_LOG_MAX_BYTES", "300")
    monkeypatch.setenv("AUDIT_LOG_KEEP", "3")
    log = AuditLogger(log_dir=tmp_path, agent_id="api", max_buffer_size=1, enable_console=False)
    for i in range(60):
        log.log_auth_failure(
            client="c", path="/q", surface="api", failures_in_window=i, throttled=False
        )
    files = list(tmp_path.glob("audit_api_*.jsonl"))
    assert 1 < len(files) <= 3, [f.name for f in files]


def test_audit_keep_zero_keeps_everything(tmp_path, monkeypatch):
    from src.runtime.audit_logger import AuditLogger

    monkeypatch.setenv("AUDIT_LOG_MAX_BYTES", "300")
    monkeypatch.setenv("AUDIT_LOG_KEEP", "0")
    log = AuditLogger(log_dir=tmp_path, agent_id="api", max_buffer_size=1, enable_console=False)
    for i in range(60):
        log.log_auth_failure(
            client="c", path="/q", surface="api", failures_in_window=i, throttled=False
        )
    assert len(list(tmp_path.glob("audit_api_*.jsonl"))) > 3


def _store(tmp_path, monkeypatch, days):
    monkeypatch.setenv("APPROVAL_RETENTION_DAYS", str(days))
    monkeypatch.delenv("STATE_DB_URL", raising=False)
    from approval import ApprovalStore

    return ApprovalStore(str(tmp_path / "a.sqlite"))


def _age(store, approval_id, days):
    when = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with store._conn() as con:
        con.execute("UPDATE approvals SET decided_at=? WHERE approval_id=?", (when, approval_id))
        con.execute(
            "UPDATE approval_events SET decided_at=? WHERE approval_id=?", (when, approval_id)
        )


def test_old_decided_items_are_purged_and_pending_ones_kept(tmp_path, monkeypatch):
    s = _store(tmp_path, monkeypatch, 0)  # no purge while setting up
    old = s.propose("t1", "brief.commit", QUESTION, "p", proposed_by="a")
    new = s.propose("t2", "brief.commit", QUESTION, "p", proposed_by="a")
    waiting = s.propose("t3", "brief.commit", QUESTION, "p", proposed_by="a")
    for a in (old, new):
        s.decide(a, "reject", decided_by="b", reason="no")
    _age(s, old, 120)
    # a store opened with a 90-day retention purges at start
    s2 = _store(tmp_path, monkeypatch, 90)
    assert s2.get(old) is None and s2.history(old) == []
    assert s2.get(new) is not None and s2.get(waiting)["status"] == "pending"


def test_retention_zero_keeps_everything(tmp_path, monkeypatch):
    s = _store(tmp_path, monkeypatch, 0)
    old = s.propose("t1", "brief.commit", QUESTION, "p", proposed_by="a")
    s.decide(old, "reject", decided_by="b", reason="no")
    _age(s, old, 3650)
    assert _store(tmp_path, monkeypatch, 0).get(old) is not None
