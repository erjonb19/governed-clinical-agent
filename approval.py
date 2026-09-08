"""
approval.py
===========
Human-in-the-loop approval for consequential agent actions.

WHY THIS EXISTS
Reads are safe, so the agent runs them autonomously. But some actions are
consequential -- committing a clinical brief to a record, escalating a case to a
care manager, sending a notification. Those must not be autonomous. The policy
engine already marks them as requiring approval (medicare_policy.yaml); this is
the surface that makes that real.

THE ARCHITECTURAL POINT
An approval is NOT a blocking function call. In any real deployment a reviewer
picks the item up minutes or days later, not while an HTTP request holds a thread
open. So the agent's execution state must OUTLIVE the request that created it.

That is what LangGraph's interrupt + checkpointer give us:
  1. the graph runs until it reaches the approval node
  2. interrupt() stops execution and PERSISTS the full state, keyed by thread_id
  3. the HTTP request returns immediately: "pending, id=X"
  4. hours later, a decision is submitted against that id
  5. the graph RESUMES from exactly where it stopped, with the decision injected

Persistence is SQLite here: durable, file-based, no server. Same tradeoff as
DuckDB vs. Databricks -- right-sized now, swappable for Postgres later by
changing the checkpointer.

DECISION TYPES (what real approval systems support)
  approve              execute as proposed
  reject               do not execute; reason recorded
  escalate             REASSIGN to a higher authority (clinician, compliance).
                       This is not a verdict -- the item stays pending and stays
                       in the queue, now owned by the person named. The paused
                       graph thread is left paused, so whoever picks it up can
                       still approve it and have the action actually run.
  approve_with_edits   reviewer modifies the proposal, THEN it executes
                       -- very common clinically: the agent drafts, the human
                          corrects a detail, then it goes

WHY ESCALATION IS A HANDOFF, NOT A CLOSE
An escalation that resolves the item is a rejection wearing a nicer word: the
action does not run, the thread is consumed, and the person named inherits
nothing but a name in an audit column. Real review queues route work; they do
not silently drop it on the floor when a reviewer says "this is above my level".
So escalate re-queues, and only approve / reject / approve_with_edits are
terminal. One proposal can therefore carry several decisions, which is why the
chain lives in its own table (approval_events) rather than the single decision
columns on the proposal row.

Deliberately NOT included: timeout auto-reject. An automatic decision on a
consequential clinical action is a poor default; better that an item sits
visible in the queue than is silently rejected.

Every decision records who, when, why, and how long it sat in the queue.
Queue latency is a real operations metric, not decoration.
"""

from __future__ import annotations

import os
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Optional, TypedDict

APPROVALS_DB = os.environ.get("APPROVALS_DB", "logs/approvals.sqlite")

# decision vocabulary
APPROVE = "approve"
REJECT = "reject"
ESCALATE = "escalate"
APPROVE_WITH_EDITS = "approve_with_edits"
DECISIONS = {APPROVE, REJECT, ESCALATE, APPROVE_WITH_EDITS}

# Which decisions let the action execute.
EXECUTES = {APPROVE, APPROVE_WITH_EDITS}

# Which decisions RESOLVE the item. Escalate is deliberately absent: it hands the
# item to someone else and leaves it pending. Callers that need to know whether
# a decision ends the graph run should test this, not EXECUTES -- a rejection
# does not execute but does end it, whereas an escalation does neither.
TERMINAL = {APPROVE, APPROVE_WITH_EDITS, REJECT}


class ApprovalStore:
    """Durable record of every proposal and every decision.

    Separate from the graph checkpointer on purpose. The checkpointer holds
    EXECUTION state (how to resume); this holds the AUDIT record (what was
    proposed, who decided, why, how long it waited). Different lifetimes,
    different consumers -- the audit record outlives the execution.
    """

    def __init__(self, path: str = APPROVALS_DB):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.path = path
        self._init()

    def _conn(self):
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        return con

    def _init(self) -> None:
        with self._conn() as con:
            con.execute("""
                CREATE TABLE IF NOT EXISTS approvals (
                    approval_id   TEXT PRIMARY KEY,
                    thread_id     TEXT NOT NULL,
                    capability    TEXT NOT NULL,
                    question      TEXT,
                    proposal      TEXT,
                    evidence      TEXT,
                    status        TEXT NOT NULL,     -- pending | approved | rejected
                    proposed_at   TEXT NOT NULL,
                    decided_at    TEXT,
                    decided_by    TEXT,
                    decision      TEXT,
                    reason        TEXT,
                    edited_proposal TEXT,
                    escalated_to  TEXT,
                    queue_seconds REAL
                )
            """)
            # The decision CHAIN. A proposal can be escalated several times
            # before it is resolved, so "who decided what" is one-to-many and
            # cannot live in the columns above -- those hold only the FINAL
            # verdict. Escalation rate is computed from here for the same
            # reason: a later approval would otherwise overwrite the evidence
            # that the item was ever escalated.
            con.execute("""
                CREATE TABLE IF NOT EXISTS approval_events (
                    event_id      INTEGER PRIMARY KEY AUTOINCREMENT,
                    approval_id   TEXT NOT NULL,
                    decision      TEXT NOT NULL,
                    decided_by    TEXT NOT NULL,
                    decided_at    TEXT NOT NULL,
                    reason        TEXT,
                    edited_proposal TEXT,
                    escalated_to  TEXT,
                    queue_seconds REAL,              -- wait for THIS hop only
                    FOREIGN KEY (approval_id) REFERENCES approvals(approval_id)
                )
            """)
            con.execute("CREATE INDEX IF NOT EXISTS idx_events_approval "
                        "ON approval_events(approval_id)")
            self._migrate(con)

    def _migrate(self, con) -> None:
        """Bring an older approvals.sqlite up to the current shape.

        The file is durable and long-lived by design, so a deployment that has
        been running carries rows written before escalation re-queued. Adding
        the column in place keeps that audit history rather than requiring the
        database to be thrown away.
        """
        cols = {r["name"] for r in con.execute("PRAGMA table_info(approvals)")}
        if "assigned_to" not in cols:
            # Current owner of a PENDING item. NULL means "unassigned" -- the
            # general queue, which is where every proposal starts.
            con.execute("ALTER TABLE approvals ADD COLUMN assigned_to TEXT")
        # Rows closed as 'escalated' by the old behaviour are exactly the items
        # that were dropped on the floor: the action never ran and nobody owned
        # them. Re-open them to the person they named, which is what escalating
        # was always supposed to mean.
        #
        # Copy the escalation into the event chain BEFORE clearing the decision
        # columns. Those columns are the only record that the escalation ever
        # happened, and re-opening the row overwrites them -- migrating an audit
        # trail must not destroy the entries it is migrating.
        con.execute("""
            INSERT INTO approval_events
                (approval_id, decision, decided_by, decided_at, reason,
                 edited_proposal, escalated_to, queue_seconds)
            SELECT a.approval_id, 'escalate', COALESCE(a.decided_by, 'unknown'),
                   COALESCE(a.decided_at, a.proposed_at), a.reason,
                   a.edited_proposal, a.escalated_to, a.queue_seconds
              FROM approvals a
             WHERE a.status = 'escalated'
               AND NOT EXISTS (SELECT 1 FROM approval_events e
                                WHERE e.approval_id = a.approval_id)
        """)
        con.execute(
            "UPDATE approvals SET status='pending', assigned_to=escalated_to, "
            "decision=NULL, decided_at=NULL, decided_by=NULL, queue_seconds=NULL "
            "WHERE status='escalated'"
        )

    # ---- proposing ----

    def propose(self, thread_id: str, capability: str, question: str,
                proposal: str, evidence: str | None = None) -> str:
        approval_id = str(uuid.uuid4())[:8]
        with self._conn() as con:
            con.execute(
                "INSERT INTO approvals (approval_id, thread_id, capability, question, "
                "proposal, evidence, status, proposed_at) VALUES (?,?,?,?,?,?,?,?)",
                (approval_id, thread_id, capability, question, proposal, evidence,
                 "pending", datetime.now(timezone.utc).isoformat()),
            )
        return approval_id

    # ---- reviewing ----

    def pending(self) -> list[dict]:
        with self._conn() as con:
            rows = con.execute(
                "SELECT * FROM approvals WHERE status = 'pending' ORDER BY proposed_at"
            ).fetchall()
        return [dict(r) for r in rows]

    def get(self, approval_id: str) -> Optional[dict]:
        with self._conn() as con:
            row = con.execute(
                "SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)
            ).fetchone()
        return dict(row) if row else None

    def find_by_thread(self, thread_id: str, status: str | None = None) -> Optional[str]:
        """Most recent approval_id for a thread, optionally filtered by status.

        Used to keep the propose node IDEMPOTENT: LangGraph re-executes a node on
        resume, and without this the store would mint a duplicate approval record
        for the same thread every time a decision came in.
        """
        q = "SELECT approval_id FROM approvals WHERE thread_id = ?"
        args: list = [thread_id]
        if status:
            q += " AND status = ?"
            args.append(status)
        q += " ORDER BY proposed_at DESC LIMIT 1"
        with self._conn() as con:
            row = con.execute(q, args).fetchone()
        return row["approval_id"] if row else None

    def decide(self, approval_id: str, decision: str, decided_by: str,
               reason: str | None = None, edited_proposal: str | None = None,
               escalated_to: str | None = None) -> dict:
        if decision not in DECISIONS:
            raise ValueError(f"unknown decision {decision!r}; expected one of {sorted(DECISIONS)}")
        item = self.get(approval_id)
        if item is None:
            raise KeyError(f"no approval {approval_id!r}")
        if item["status"] != "pending":
            raise ValueError(f"approval {approval_id} already {item['status']}")
        if decision == APPROVE_WITH_EDITS and not edited_proposal:
            raise ValueError("approve_with_edits requires edited_proposal")
        if decision == ESCALATE and not escalated_to:
            raise ValueError("escalate requires escalated_to")

        now = datetime.now(timezone.utc)
        proposed = datetime.fromisoformat(item["proposed_at"])
        # Total wait, from proposal to this decision. On a re-queued item that
        # spans every hop, which is the number ops actually cares about: how
        # long the work sat, not how long each individual reviewer held it.
        queue_seconds = (now - proposed).total_seconds()
        # Per-hop wait: since the previous decision on this item, else proposal.
        last_at = self._last_event_at(approval_id) or item["proposed_at"]
        hop_seconds = (now - datetime.fromisoformat(last_at)).total_seconds()

        with self._conn() as con:
            con.execute(
                "INSERT INTO approval_events (approval_id, decision, decided_by, "
                "decided_at, reason, edited_proposal, escalated_to, queue_seconds) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (approval_id, decision, decided_by, now.isoformat(), reason,
                 edited_proposal, escalated_to, hop_seconds),
            )
            if decision == ESCALATE:
                # A HANDOFF, not a verdict. The item stays pending so it stays
                # in pending(), and assigned_to names who owns it now. The
                # decision columns stay empty because it has not been decided --
                # writing the escalation there is what made the old behaviour
                # look like a resolution.
                con.execute(
                    "UPDATE approvals SET assigned_to=?, escalated_to=? "
                    "WHERE approval_id=?",
                    (escalated_to, escalated_to, approval_id),
                )
            else:
                status = {APPROVE: "approved", APPROVE_WITH_EDITS: "approved",
                          REJECT: "rejected"}[decision]
                con.execute(
                    "UPDATE approvals SET status=?, decided_at=?, decided_by=?, "
                    "decision=?, reason=?, edited_proposal=?, queue_seconds=? "
                    "WHERE approval_id=?",
                    (status, now.isoformat(), decided_by, decision, reason,
                     edited_proposal, queue_seconds, approval_id),
                )
        return self.get(approval_id)

    def _last_event_at(self, approval_id: str) -> Optional[str]:
        with self._conn() as con:
            row = con.execute(
                "SELECT decided_at FROM approval_events WHERE approval_id = ? "
                "ORDER BY event_id DESC LIMIT 1", (approval_id,)
            ).fetchone()
        return row["decided_at"] if row else None

    def history(self, approval_id: str) -> list[dict]:
        """Every decision taken on this proposal, oldest first.

        The proposal row holds only the final verdict; an item escalated twice
        and then approved has three entries here and one there.
        """
        with self._conn() as con:
            rows = con.execute(
                "SELECT * FROM approval_events WHERE approval_id = ? "
                "ORDER BY event_id", (approval_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ---- ops metrics ----

    def metrics(self) -> dict:
        """Approval-queue health. Escalation rate and queue latency are the
        signals that matter operationally -- the original spec called for
        escalation rate as an AIOps metric.

        Escalations are counted from the EVENT CHAIN, not from the proposal's
        decision column. An escalated item is still pending and will later carry
        a different final decision, so counting the columns would report an
        escalation rate of zero on exactly the queue where escalation is
        happening most.
        """
        with self._conn() as con:
            total = con.execute("SELECT count(*) c FROM approvals").fetchone()["c"]
            by_status = {r["status"]: r["c"] for r in con.execute(
                "SELECT status, count(*) c FROM approvals GROUP BY status").fetchall()}
            decided = con.execute(
                "SELECT count(*) c, avg(queue_seconds) a FROM approvals "
                "WHERE queue_seconds IS NOT NULL").fetchone()
            by_decision = {r["decision"]: r["c"] for r in con.execute(
                "SELECT decision, count(*) c FROM approvals "
                "WHERE decision IS NOT NULL GROUP BY decision").fetchall()}
            # Proposals that were escalated at least once, ever.
            n_esc = con.execute(
                "SELECT count(DISTINCT approval_id) c FROM approval_events "
                "WHERE decision = ?", (ESCALATE,)).fetchone()["c"]
            assigned = con.execute(
                "SELECT count(*) c FROM approvals "
                "WHERE status='pending' AND assigned_to IS NOT NULL").fetchone()["c"]
        n_decided = decided["c"] or 0
        # Escalation is a property of a proposal's LIFETIME, so the denominator
        # is every proposal that has reached a verdict -- not the resolved-item
        # count, which would let a still-open escalated item push the rate above 1.
        return {
            "total_proposals": total,
            "by_status": by_status,
            "by_decision": by_decision,
            "pending": by_status.get("pending", 0),
            "pending_assigned": assigned,
            "escalated_ever": n_esc,
            "avg_queue_seconds": round(decided["a"], 2) if decided["a"] is not None else None,
            "escalation_rate": round(n_esc / total, 4) if total else None,
            "approval_rate": round(
                (by_decision.get(APPROVE, 0) + by_decision.get(APPROVE_WITH_EDITS, 0)) / n_decided, 4
            ) if n_decided else None,
        }
