"""Escalation is a HANDOFF, not a verdict.

WHY THIS FILE EXISTS
`decide()` used to write status='escalated', which dropped the row out of
`pending()`. Escalating therefore CLOSED the item: the action never ran, the
person named in `escalated_to` inherited nothing but an audit column, and
`resume()` consumed the paused graph thread on the way out -- so even if that
person had been found, there was no longer an interrupt for their approval to
resume. Escalate was a rejection wearing a nicer word.

The approval queue had no test coverage at all, which is how a decision type
could be inert in shipped code and still look right in the UI, the API, and the
demo script.

These tests pin the corrected behaviour: escalate re-queues to a new owner,
leaves the thread paused, and the new owner's approval still executes.
"""

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from approval import (ApprovalStore, APPROVE, APPROVE_WITH_EDITS, ESCALATE,
                      REJECT, EXECUTES, TERMINAL)


@pytest.fixture
def store(tmp_path):
    return ApprovalStore(str(tmp_path / "approvals.sqlite"))


@pytest.fixture
def item(store):
    return store.propose("thread-1", "brief.commit", "Which hospitals?", "a draft")


# --------------------------------------------------------------------------
# The core defect
# --------------------------------------------------------------------------

def test_escalated_item_stays_in_the_queue(store, item):
    """The whole bug in one assertion: escalating must not empty the queue."""
    store.decide(item, ESCALATE, "nurse.a", escalated_to="dr.b")
    assert [p["approval_id"] for p in store.pending()] == [item]


def test_escalation_assigns_an_owner(store, item):
    store.decide(item, ESCALATE, "nurse.a", escalated_to="dr.b")
    row = store.get(item)
    assert row["assigned_to"] == "dr.b"
    assert row["status"] == "pending"


def test_escalation_is_not_recorded_as_a_verdict(store, item):
    """The decision columns hold the FINAL verdict. An escalated item has none
    yet -- filling them in is exactly what made escalation look resolved."""
    store.decide(item, ESCALATE, "nurse.a", escalated_to="dr.b")
    row = store.get(item)
    assert row["decision"] is None
    assert row["decided_at"] is None
    assert row["decided_by"] is None


def test_new_owner_can_still_approve(store, item):
    store.decide(item, ESCALATE, "nurse.a", escalated_to="dr.b")
    rec = store.decide(item, APPROVE, "dr.b", reason="reviewed")
    assert rec["status"] == "approved"
    assert rec["decided_by"] == "dr.b"
    assert store.pending() == []


def test_escalation_can_chain(store, item):
    """Two hops before a verdict. The single decision column cannot express
    this, which is why the chain has its own table."""
    store.decide(item, ESCALATE, "nurse.a", escalated_to="dr.b")
    store.decide(item, ESCALATE, "dr.b", escalated_to="compliance.c")
    assert store.get(item)["assigned_to"] == "compliance.c"

    store.decide(item, APPROVE, "compliance.c")
    chain = [(e["decision"], e["decided_by"]) for e in store.history(item)]
    assert chain == [("escalate", "nurse.a"),
                     ("escalate", "dr.b"),
                     ("approve", "compliance.c")]


def test_escalate_still_requires_a_target(store, item):
    with pytest.raises(ValueError, match="escalated_to"):
        store.decide(item, ESCALATE, "nurse.a")


# --------------------------------------------------------------------------
# Terminal decisions still close the item
# --------------------------------------------------------------------------

@pytest.mark.parametrize("decision,expected,kwargs", [
    (APPROVE, "approved", {}),
    (REJECT, "rejected", {}),
    (APPROVE_WITH_EDITS, "approved", {"edited_proposal": "corrected draft"}),
])
def test_terminal_decisions_resolve(store, item, decision, expected, kwargs):
    rec = store.decide(item, decision, "dr.b", **kwargs)
    assert rec["status"] == expected
    assert store.pending() == []


def test_a_resolved_item_cannot_be_decided_twice(store, item):
    store.decide(item, REJECT, "dr.b")
    with pytest.raises(ValueError, match="already rejected"):
        store.decide(item, APPROVE, "dr.c")


def test_decision_sets_disagree_only_on_reject(store):
    """EXECUTES answers 'does the action run', TERMINAL answers 'is it over'.
    Reject is the case that separates them; escalate is in neither."""
    assert ESCALATE not in EXECUTES and ESCALATE not in TERMINAL
    assert REJECT in TERMINAL and REJECT not in EXECUTES
    assert EXECUTES < TERMINAL


# --------------------------------------------------------------------------
# Metrics must survive the re-queue
# --------------------------------------------------------------------------

def test_escalation_rate_survives_a_later_approval(store, item):
    """Counting escalations from the proposal's decision column reports zero on
    exactly the queue where escalation happens most: the final approval
    overwrites the only evidence it occurred."""
    store.decide(item, ESCALATE, "nurse.a", escalated_to="dr.b")
    store.decide(item, APPROVE, "dr.b")
    m = store.metrics()
    assert m["escalated_ever"] == 1
    assert m["escalation_rate"] == 1.0
    assert m["by_decision"].get("approve") == 1


def test_escalation_rate_never_exceeds_one(store):
    """An open escalated item has no verdict. If the denominator were resolved
    items only, a queue mid-escalation would divide by zero or exceed 1."""
    a = store.propose("t-a", "brief.commit", "Q1", "d1")
    store.propose("t-b", "brief.commit", "Q2", "d2")
    store.decide(a, ESCALATE, "nurse.a", escalated_to="dr.b")
    m = store.metrics()
    assert m["escalation_rate"] == 0.5
    assert m["pending_assigned"] == 1


def test_queue_seconds_measures_the_whole_wait(store, item):
    """Total time to resolution, across every hop -- how long the work sat, not
    how long the last reviewer held it."""
    store.decide(item, ESCALATE, "nurse.a", escalated_to="dr.b")
    rec = store.decide(item, APPROVE, "dr.b")
    hops = [e["queue_seconds"] for e in store.history(item)]
    assert rec["queue_seconds"] >= max(hops)
    assert rec["queue_seconds"] == pytest.approx(sum(hops), abs=0.05)


# --------------------------------------------------------------------------
# Existing deployments
# --------------------------------------------------------------------------

OLD_SCHEMA = """
CREATE TABLE approvals (
    approval_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, capability TEXT NOT NULL,
    question TEXT, proposal TEXT, evidence TEXT, status TEXT NOT NULL,
    proposed_at TEXT NOT NULL, decided_at TEXT, decided_by TEXT, decision TEXT,
    reason TEXT, edited_proposal TEXT, escalated_to TEXT, queue_seconds REAL)
"""


def _legacy_db(path):
    con = sqlite3.connect(path)
    con.execute(OLD_SCHEMA)
    con.execute(
        "INSERT INTO approvals VALUES ('old1','t9','brief.commit','Q','draft',NULL,"
        "'escalated','2026-09-01T00:00:00+00:00','2026-09-01T00:05:00+00:00',"
        "'nurse.a','escalate','needs a clinician',NULL,'dr.b',300.0)")
    con.commit()
    con.close()


def test_legacy_escalated_rows_are_reopened(tmp_path):
    """Items closed as 'escalated' by the old code are the ones that were
    dropped on the floor -- nobody owned them and the action never ran."""
    path = str(tmp_path / "old.sqlite")
    _legacy_db(path)

    store = ApprovalStore(path)
    pend = store.pending()
    assert [p["approval_id"] for p in pend] == ["old1"]
    assert pend[0]["assigned_to"] == "dr.b"


def test_migration_preserves_the_original_escalation(tmp_path):
    """Re-opening the row overwrites the decision columns, which are the only
    record the escalation happened. It has to be copied out first."""
    path = str(tmp_path / "old.sqlite")
    _legacy_db(path)

    store = ApprovalStore(path)
    assert [(e["decision"], e["decided_by"], e["escalated_to"])
            for e in store.history("old1")] == [("escalate", "nurse.a", "dr.b")]
    assert store.metrics()["escalated_ever"] == 1


def test_migration_is_idempotent(tmp_path):
    """Opening the store is not a one-shot upgrade step -- every process that
    constructs an ApprovalStore runs it."""
    path = str(tmp_path / "old.sqlite")
    _legacy_db(path)

    ApprovalStore(path)
    store = ApprovalStore(path)
    ApprovalStore(path)
    assert len(store.history("old1")) == 1


def test_legacy_item_can_finally_be_approved(tmp_path):
    path = str(tmp_path / "old.sqlite")
    _legacy_db(path)

    store = ApprovalStore(path)
    rec = store.decide("old1", APPROVE, "dr.b", reason="picked up at last")
    assert rec["status"] == "approved"
    assert store.pending() == []
