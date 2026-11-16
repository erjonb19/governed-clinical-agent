"""Audit item 32: reviewer identity and separation of duties in the approval queue.

Every caller shares one API key until per-user keys (item 22), so a reviewer is a
typed name. Each decision is therefore stored as "self-asserted", and the rules
that can hold on names do: nobody decides their own proposal, an escalated item is
decided only by the person it went to, and an edit cannot add a figure the
evidence does not contain.
"""

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from approval import ApprovalStore  # noqa: E402
from groundedness import ungrounded_numbers  # noqa: E402


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("STATE_DB_URL", raising=False)
    return ApprovalStore(str(tmp_path / "a.sqlite"))


def _item(store, proposed_by="A. Analyst"):
    return store.propose(
        thread_id="t1",
        capability="brief.commit",
        question="Which hospitals have the worst HF readmission rate?",
        proposal="Proposed brief.commit\nEvidence: 3 rows returned; leading result: "
        "{'facility_name': 'X', 'hf_readmission_rate': 22.4}",
        evidence="[{'facility_name': 'X', 'hf_readmission_rate': 22.4}, "
        "{'facility_name': 'Y', 'hf_readmission_rate': 21.9}]",
        proposed_by=proposed_by,
    )


def test_the_proposer_cannot_decide_their_own_proposal(store):
    aid = _item(store)
    for same in ("A. Analyst", " a.  analyst ", "A. ANALYST"):
        with pytest.raises(ValueError, match="person who proposed it"):
            store.decide(aid, "approve", same)
    assert store.get(aid)["status"] == "pending"
    out = store.decide(aid, "approve", "B. Reviewer")
    assert out["status"] == "approved" and out["identity"] == "self-asserted"


def test_every_decision_is_recorded_as_self_asserted(store):
    aid = _item(store)
    store.decide(aid, "reject", "B. Reviewer", reason="not now")
    con = sqlite3.connect(store.path)
    assert (
        con.execute("SELECT identity FROM approval_events WHERE approval_id=?", (aid,)).fetchone()[
            0
        ]
        == "self-asserted"
    )


def test_an_escalated_item_is_decided_only_by_its_new_owner(store):
    aid = _item(store)
    store.decide(aid, "escalate", "B. Reviewer", escalated_to="dr.oncall")
    with pytest.raises(ValueError, match="only they can decide it"):
        store.decide(aid, "approve", "B. Reviewer")
    assert store.decide(aid, "approve", "Dr.OnCall")["status"] == "approved"


def test_an_edit_cannot_add_a_figure_the_evidence_does_not_have(store):
    aid = _item(store)
    with pytest.raises(ValueError, match="not in the evidence"):
        store.decide(
            aid,
            "approve_with_edits",
            "B. Reviewer",
            edited_proposal="Facility X readmits 31.5% of HF patients.",
        )
    out = store.decide(
        aid,
        "approve_with_edits",
        "B. Reviewer",
        edited_proposal="Facility X has the worst HF readmission rate (22.4%), Y next (21.9).",
    )
    assert out["status"] == "approved"


def test_a_decision_needs_a_name(store):
    with pytest.raises(ValueError, match="decided_by is required"):
        store.decide(_item(store), "approve", "   ")


def test_an_older_database_gets_the_new_columns(tmp_path, monkeypatch):
    monkeypatch.delenv("STATE_DB_URL", raising=False)
    path = str(tmp_path / "old.sqlite")
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE approvals (approval_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, "
        "capability TEXT NOT NULL, question TEXT, proposal TEXT, evidence TEXT, "
        "status TEXT NOT NULL, proposed_at TEXT NOT NULL, decided_at TEXT, decided_by TEXT, "
        "decision TEXT, reason TEXT, edited_proposal TEXT, escalated_to TEXT, queue_seconds REAL)"
    )
    con.execute(
        "CREATE TABLE approval_events (event_id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "approval_id TEXT NOT NULL, decision TEXT NOT NULL, decided_by TEXT NOT NULL, "
        "decided_at TEXT NOT NULL, reason TEXT, edited_proposal TEXT, escalated_to TEXT, "
        "queue_seconds REAL)"
    )
    con.commit()
    con.close()
    store = ApprovalStore(path)
    aid = _item(store, proposed_by=None)  # an anonymous proposal still works
    assert store.decide(aid, "approve", "B. Reviewer")["status"] == "approved"


def test_numbers_in_free_text():
    assert ungrounded_numbers("1,250 patients, 22.4%", ["22.4 and 1250"]) == []
    assert ungrounded_numbers("rate 9.9", ["rate 22.4"]) == [9.9]
