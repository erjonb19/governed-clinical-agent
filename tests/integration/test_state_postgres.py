"""Durable state in Postgres (backlog item 19).

These run only with STATE_DB_URL pointing at a Postgres (the CI job's service
container); the rest of the suite runs against both backends.

"A redeploy" here is what Render does: the process and its disk are gone, a
new one starts with an empty local directory and the same STATE_DB_URL.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import state_db

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_STATE_DB_URL"),
    reason="needs TEST_STATE_DB_URL=postgresql://.../<name containing test> (CI job)",
)


def test_an_approval_survives_a_redeploy(tmp_path):
    from approval import ApprovalStore

    before = ApprovalStore(str(tmp_path / "old_disk" / "approvals.sqlite"))
    aid = before.propose("t1", "brief.commit", "commit the brief?", "draft", None)
    # the old disk is gone; a new instance starts on an empty one
    after = ApprovalStore(str(tmp_path / "new_disk" / "approvals.sqlite"))
    assert [p["approval_id"] for p in after.pending()] == [aid]
    after.decide(aid, "approve", "dr.b")
    assert ApprovalStore(str(tmp_path / "third" / "a.sqlite")).get(aid)["status"] == "approved"
    assert not (tmp_path / "new_disk" / "approvals.sqlite").exists()


def test_the_decision_chain_survives_too(tmp_path):
    from approval import ApprovalStore

    s = ApprovalStore(str(tmp_path / "a" / "x.sqlite"))
    aid = s.propose("t2", "brief.commit", "q", "p", None)
    s.decide(aid, "escalate", "nurse.a", reason="needs a clinician", escalated_to="dr.b")
    hist = ApprovalStore(str(tmp_path / "b" / "y.sqlite")).history(aid)
    assert [h["decision"] for h in hist] == ["escalate"]


def test_health_names_the_store_without_credentials():
    # /health is unauthenticated: the kind of store only, no host or credentials
    assert state_db.describe() == "postgres"


def test_old_decided_items_are_purged_on_postgres_too(tmp_path, monkeypatch):
    # backlog item 60: the DELETE ... IN (SELECT ...) purge, on Postgres
    from datetime import datetime, timedelta, timezone

    from approval import ApprovalStore

    monkeypatch.setenv("APPROVAL_RETENTION_DAYS", "0")
    s = ApprovalStore(str(tmp_path / "p" / "a.sqlite"))
    old = s.propose("tp1", "brief.commit", "q", "p", None, proposed_by="a")
    esc = s.propose("tp2", "brief.commit", "q", "p", None, proposed_by="a")
    s.decide(old, "reject", "b", reason="no")
    s.decide(esc, "escalate", "b", reason="later", escalated_to="c")
    when = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
    with s._conn() as con:
        con.execute("UPDATE approvals SET decided_at=? WHERE approval_id=?", (when, old))
        con.execute(
            "UPDATE approval_events SET decided_at=? WHERE approval_id IN (?, ?)", (when, old, esc)
        )
    monkeypatch.setenv("APPROVAL_RETENTION_DAYS", "90")
    after = ApprovalStore(str(tmp_path / "q" / "b.sqlite"))
    assert after.get(old) is None and after.history(old) == []
    assert after.get(esc)["status"] == "pending"  # escalated: still waiting, never purged
