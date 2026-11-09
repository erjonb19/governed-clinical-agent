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
    not state_db.is_postgres(), reason="needs STATE_DB_URL=postgresql://... (CI job)"
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
    d = state_db.describe()
    creds = os.environ["STATE_DB_URL"].split("//", 1)[1].split("@", 1)[0]  # user:password
    assert d.startswith("postgres://") and "@" not in d and creds not in d
