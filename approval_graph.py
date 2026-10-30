"""
approval_graph.py
=================
The governed agent WITH a human-in-the-loop checkpoint.

Extends the self-correcting graph (agent_graph.py) with a fifth node: after the
agent queries the data and drafts a consequential action, execution STOPS and
waits for a human.

    plan -> execute -> route --(success)--> propose -> [INTERRUPT] -> commit
     ^                   |                                              |
     +---(retry w/ reason)                                        approve? execute
                                                                  reject?  stop

WHAT MAKES THIS REAL RATHER THAN DECORATIVE
The interrupt is a true pause, not a blocking call. LangGraph's checkpointer
persists the entire graph state to SQLite keyed by thread_id. The caller gets
back "pending, id=X" immediately. Hours or days later a decision is submitted and
the graph RESUMES from exactly where it stopped.

That is the difference between an approval QUEUE and a function that waits. No
production system holds a request thread open for a human.

TWO LAYERS OF CONTROL, DELIBERATELY DISTINCT
  sql_guard / policy  -> governs whether a TOOL CALL may run (reads are safe,
                         so they are autonomous)
  this checkpoint     -> governs whether a CONSEQUENTIAL ACTION may happen
                         (commit a brief, escalate a case, notify a care team)
Risk-tiering only means something if the safe tier really is autonomous. The
agent queries freely; it cannot ACT freely.

Usage:
    python approval_graph.py          # end-to-end demo: propose, queue, decide, resume
"""

import contextlib
import os
import sys
import uuid
from typing import Any, Optional, TypedDict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt, Command
from langgraph.checkpoint.sqlite import SqliteSaver

import tracing
from src.runtime.agent_runtime import AgentRuntime
from analytics_query_tool import AnalyticsQueryTool
from nl_to_sql_planner import NLToSQLPlanner, SCHEMA_DOC
import schema_check
from approval import (ApprovalStore, APPROVE, REJECT, ESCALATE,
                      APPROVE_WITH_EDITS, EXECUTES, TERMINAL)

GOLD_DB = os.environ.get("HOSPITAL_GOLD_DB", "medallion/hospital_gold.duckdb")
POLICY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "medicare_policy.yaml")
CHECKPOINT_DB = os.environ.get("CHECKPOINT_DB", "logs/checkpoints.sqlite")
CAPABILITY = "analytics.query_aggregate"
MAX_ATTEMPTS = 3
# Rows the reviewer sees as evidence. Only these are kept in the checkpointed
# state: _propose re-runs on resume (possibly hours later, in another process)
# and needs them, and the approval record stores the same rows anyway. The rest
# of the result never reaches the checkpoint DB, which nothing prunes.
EVIDENCE_ROWS = 3


class ApprovalState(TypedDict, total=False):
    question: str
    thread_id: str
    capability: str              # the consequential capability being proposed
    sql: Optional[str]
    attempts: int
    last_error: Optional[str]
    allowed: bool
    decided_by: Optional[str]
    reason: Optional[str]
    safe_sql: Optional[str]
    rows: Optional[list]
    row_count: Optional[int]
    proposal: Optional[str]      # the draft the human reviews
    approval_id: Optional[str]
    human_decision: Optional[dict]
    committed: bool
    trajectory: list


class GovernedApprovalAgent:
    def __init__(self, db_path: str = GOLD_DB, capability: str = "brief.commit",
                 runtime=None, planner=None,
                 tracer: Optional[tracing.Tracer] = None):
        """
        runtime / planner: pass in an ALREADY-BUILT runtime and planner to share
        them with a host application.

        Why this matters: the tool registry is process-global -- a capability can
        only be registered once. When the API server has already registered
        analytics.query_aggregate for /query, this agent must REUSE that runtime
        rather than build a second one, or registration raises. Standalone use
        (running this file directly) still constructs its own.
        """
        self.capability = capability
        if runtime is not None:
            self.runtime = runtime
            tool = None
        else:
            self.runtime = AgentRuntime()
            self.runtime.load_policy(POLICY_PATH)
            tool = AnalyticsQueryTool(db_path=db_path, seed_demo=False)
            self.runtime.register_tool(tool)
        self.planner = planner or NLToSQLPlanner()
        if tool is not None:
            schema_check.verify(SCHEMA_DOC, tool._con,
                                schema_label=os.environ.get("PLANNER_SCHEMA", "hospital"),
                                db_label=db_path)
        self.store = ApprovalStore()
        self.tracer = tracer or tracing.get_tracer()
        os.makedirs(os.path.dirname(CHECKPOINT_DB) or ".", exist_ok=True)
        # the checkpointer is what makes state outlive the request
        self._cm = SqliteSaver.from_conn_string(CHECKPOINT_DB)
        self.checkpointer = self._cm.__enter__()
        self.graph = self._build()

    def _traced(self, state: ApprovalState):
        """Attach the thread's trace (trace id = thread id) around a node."""
        tid = state.get("thread_id")
        if tid and tracing.current_trace_id() != tid:
            return self.tracer.attach(tid)
        return contextlib.nullcontext(tid)

    # ---------------- nodes ----------------

    def _plan(self, state: ApprovalState) -> dict:
        attempt = state.get("attempts", 0) + 1
        prompt = state["question"]
        if state.get("last_error"):
            prompt = (f"{state['question']}\n\nYour previous SQL was REJECTED:\n"
                      f"{state['last_error']}\n\nWrite a corrected query.")
        with self._traced(state), self.tracer.span(
                "plan", attempt=attempt, retry_context=bool(state.get("last_error"))) as sp:
            sql, m = self.planner.generate_sql_with_metrics(prompt)
            sp.set(sql=sql, sql_sha256=tracing.sql_sha256(sql),
                   model=getattr(m, "model", None), provider=getattr(m, "provider", None),
                   prompt_tokens=getattr(m, "prompt_tokens", None),
                   completion_tokens=getattr(m, "completion_tokens", None),
                   est_cost_usd=getattr(m, "est_cost_usd", None),
                   latency_ms=getattr(m, "latency_ms", None))
        return {"sql": sql, "attempts": attempt,
                "trajectory": state.get("trajectory", []) + [
                    {"step": "plan", "attempt": attempt, "sql": sql[:120]}]}

    def _execute(self, state: ApprovalState) -> dict:
        sql = state.get("sql") or ""
        with self._traced(state), self.tracer.span(
                "guard", attempt=state.get("attempts"), sql_sha256=tracing.sql_sha256(sql)) as g:
            result = self.runtime.execute_tool(CAPABILITY, {"sql": sql})
            if not getattr(result, "allowed", False):
                reason = getattr(result, "explanation", None) or "not permitted"
                g.set(allowed=False, decided_by="policy", reason=reason[:500])
                return {"allowed": False, "reason": reason, "last_error": reason,
                        "trajectory": state.get("trajectory", []) + [
                            {"step": "execute", "outcome": "denied", "reason": reason[:120]}]}
            tr = result.result
            if tr is None or not getattr(tr, "success", False):
                err = getattr(tr, "error", "no result") if tr else "no result"
                g.set(allowed=False, decided_by="guard", reason=(err or "")[:500])
                return {"allowed": False, "reason": err, "last_error": err,
                        "trajectory": state.get("trajectory", []) + [
                            {"step": "execute", "outcome": "failed", "reason": err[:120]}]}
            out = tr.output or {}
            g.set(allowed=True, decided_by="executed")
            self.tracer.event("execute", attempt=state.get("attempts"),
                              row_count=out.get("row_count"), backend=out.get("backend"))
        return {"allowed": True, "reason": None, "last_error": None,
                "safe_sql": out.get("safe_sql"),
                "rows": (out.get("rows") or [])[:EVIDENCE_ROWS],
                "row_count": out.get("row_count"),
                "trajectory": state.get("trajectory", []) + [
                    {"step": "execute", "outcome": "success",
                     "row_count": out.get("row_count")}]}

    def _propose(self, state: ApprovalState) -> dict:
        """Draft the consequential action and PAUSE for a human.

        The draft is grounded in rows the query actually returned -- the agent
        cannot propose acting on a number it did not retrieve.
        """
        rows = state.get("rows") or []
        summary = f"{state.get('row_count', 0)} rows returned"
        if rows:
            summary += f"; leading result: {rows[0]}"
        proposal = (f"Proposed {self.capability} for: {state['question']}\n"
                    f"Evidence: {summary}")

        # IDEMPOTENCY. On resume, LangGraph RE-EXECUTES this node from the top --
        # that is how interrupt/resume works: the node runs again and interrupt()
        # returns the decision instead of pausing. Creating the approval record
        # unconditionally would therefore mint a SECOND record for the same
        # thread on every resume, corrupting the audit trail and making the
        # thread->approval lookup ambiguous.
        #
        # So: reuse the record this thread already has, if any.
        thread_id = state.get("thread_id", "unknown")
        existing = state.get("approval_id") or self.store.find_by_thread(thread_id)
        if existing:
            approval_id = existing
        else:
            approval_id = self.store.propose(
                thread_id=thread_id,
                capability=self.capability,
                question=state["question"],
                proposal=proposal,
                evidence=str(rows[:EVIDENCE_ROWS]),
            )
            with self._traced(state):
                # recorded once, when the record is minted; the re-execution on
                # resume takes the `existing` branch above
                self.tracer.event("approval.propose", approval_id=approval_id,
                                  capability=self.capability, row_count=state.get("row_count"))

        # THE PAUSE. Execution stops here and state is persisted. Whatever is
        # passed to interrupt() is surfaced to the caller as the pending payload.
        decision = interrupt({
            "approval_id": approval_id,
            "capability": self.capability,
            "proposal": proposal,
            "evidence_rows": rows[:EVIDENCE_ROWS],
        })

        # --- everything below runs only AFTER a decision resumes the graph ---
        return {"approval_id": approval_id, "human_decision": decision,
                "proposal": proposal,
                "trajectory": state.get("trajectory", []) + [
                    {"step": "propose", "approval_id": approval_id,
                     "outcome": "awaited_human"}]}

    def _commit(self, state: ApprovalState) -> dict:
        """Execute the action only if a human approved it."""
        decision = (state.get("human_decision") or {})
        verdict = decision.get("decision")
        who = decision.get("decided_by", "unknown")

        with self._traced(state):
            self.tracer.event("answer", outcome="committed" if verdict in EXECUTES
                              else "not_committed", verdict=verdict, decided_by=f"human:{who}")
        if verdict in EXECUTES:
            final = decision.get("edited_proposal") or state.get("proposal")
            return {"committed": True, "decided_by": f"human:{who}",
                    "proposal": final,
                    "trajectory": state.get("trajectory", []) + [
                        {"step": "commit", "outcome": "committed",
                         "verdict": verdict, "by": who}]}
        return {"committed": False, "decided_by": f"human:{who}",
                "reason": decision.get("reason"),
                "trajectory": state.get("trajectory", []) + [
                    {"step": "commit", "outcome": "not_committed",
                     "verdict": verdict, "by": who}]}

    # ---------------- routing ----------------

    def _route(self, state: ApprovalState) -> str:
        if state.get("allowed"):
            return "propose"
        if state.get("attempts", 0) < MAX_ATTEMPTS:
            return "plan"
        return "end"

    def _build(self):
        g = StateGraph(ApprovalState)
        g.add_node("plan", self._plan)
        g.add_node("execute", self._execute)
        g.add_node("propose", self._propose)
        g.add_node("commit", self._commit)
        g.add_edge(START, "plan")
        g.add_edge("plan", "execute")
        g.add_conditional_edges("execute", self._route,
                                {"plan": "plan", "propose": "propose", "end": END})
        g.add_edge("propose", "commit")
        g.add_edge("commit", END)
        return g.compile(checkpointer=self.checkpointer)

    # ---------------- public API ----------------

    def state(self, thread_id: str) -> dict:
        """Where the checkpointed thread stands: parked at the interrupt, or done."""
        snap = self.graph.get_state({"configurable": {"thread_id": thread_id}})
        values = getattr(snap, "values", None) or {}
        if not values:
            return {"thread_id": thread_id, "exists": False}
        nxt = list(getattr(snap, "next", ()) or ())
        return {
            "thread_id": thread_id, "exists": True, "finished": not nxt, "next": nxt,
            "attempts": values.get("attempts", 0), "last_error": values.get("last_error"),
            "approval_id": values.get("approval_id"), "committed": values.get("committed"),
            "steps": len(values.get("trajectory") or []),
        }

    def start(self, question: str, thread_id: str | None = None) -> dict:
        """Run until the approval checkpoint. Returns immediately with a pending id."""
        thread_id = thread_id or str(uuid.uuid4())[:8]
        cfg = {"configurable": {"thread_id": thread_id}}
        # trace id = thread id: a decision submitted hours later appends to this trace
        # preserved(): start_trace makes the approval trace current, and attach()
        # alone would "restore" it on exit, leaving it current for the caller
        with self.tracer.preserved(), self.tracer.attach(self.tracer.start_trace(
                "approval", trace_id=thread_id, question=question,
                capability=self.capability)):
            try:
                state = self.graph.invoke(
                    {"question": question, "thread_id": thread_id, "attempts": 0,
                     "trajectory": [], "committed": False}, cfg)
            except BaseException as exc:
                self.tracer.end_trace(thread_id, status="error",
                                      error=f"{type(exc).__name__}: {exc}"[:500])
                raise
            interrupts = state.get("__interrupt__")
            if interrupts:
                payload = interrupts[0].value
                # still open: the trace ends when a terminal decision resumes it
                self.tracer.end_trace(thread_id, status="pending_approval",
                                      approval_id=payload.get("approval_id"))
                return {"status": "pending_approval", "thread_id": thread_id,
                        "trace_id": thread_id, **payload}
            self.tracer.end_trace(thread_id, status="ok", attempts=state.get("attempts"),
                                  reason=(state.get("reason") or "")[:500])
            return {"status": "completed", "thread_id": thread_id, "trace_id": thread_id,
                    **state}

    def resume(self, thread_id: str, decision: str, decided_by: str,
               reason: str | None = None, edited_proposal: str | None = None,
               escalated_to: str | None = None) -> dict:
        """Record the decision and, if it is terminal, RESUME the paused graph.

        ESCALATION DOES NOT RESUME. Resuming consumes the interrupt: the graph
        runs on to `commit`, sees a verdict that is not in EXECUTES, finishes as
        not-committed, and the thread is spent. There is then nothing left for
        the person the item was escalated TO to approve -- their approval would
        have no execution to authorise.

        So an escalation records the handoff and returns with the thread still
        parked at the interrupt, exactly as it was. The item stays pending, now
        owned by someone else, and their eventual approve/reject resumes it.
        """
        approval_id = self.store.find_by_thread(thread_id, status="pending")
        if not approval_id:
            raise KeyError(f"no pending approval for thread {thread_id}")
        record = self.store.decide(approval_id, decision, decided_by,
                                   reason=reason, edited_proposal=edited_proposal,
                                   escalated_to=escalated_to)

        with self.tracer.attach(thread_id):
            self.tracer.event("approval.decision", approval_id=approval_id,
                              decision=decision, decided_by=decided_by,
                              terminal=decision in TERMINAL,
                              escalated_to=escalated_to, reason=(reason or "")[:500])
        if decision not in TERMINAL:
            return {"status": "reassigned", "approval_record": record,
                    "committed": False, "assigned_to": record.get("assigned_to"),
                    "still_pending": True,
                    "proposal": record.get("proposal"),
                    "trajectory": []}

        cfg = {"configurable": {"thread_id": thread_id}}
        with self.tracer.attach(thread_id):
            try:
                state = self.graph.invoke(Command(resume={
                    "decision": decision, "decided_by": decided_by, "reason": reason,
                    "edited_proposal": edited_proposal, "escalated_to": escalated_to,
                }), cfg)
            except BaseException as exc:
                self.tracer.end_trace(thread_id, status="error",
                                      error=f"{type(exc).__name__}: {exc}"[:500])
                raise
            self.tracer.end_trace(thread_id, status="ok", decision=decision,
                                  committed=bool(state.get("committed")))
        return {"status": "resumed", "approval_record": record,
                "committed": state.get("committed"),
                "still_pending": False,
                "proposal": state.get("proposal"),
                "trajectory": state.get("trajectory", [])}


def _demo():
    if not os.path.exists(GOLD_DB):
        sys.exit(f"missing {GOLD_DB}")
    agent = GovernedApprovalAgent()

    print("=" * 72)
    print("1. AGENT PROPOSES -- runs the query, drafts an action, then STOPS")
    started = agent.start(
        "Which hospitals have the worst heart failure readmission rates?")
    print(f"   status:      {started['status']}")
    print(f"   thread_id:   {started['thread_id']}")
    print(f"   approval_id: {started.get('approval_id')}")
    print(f"   proposal:    {str(started.get('proposal'))[:90]}...")

    print()
    print("=" * 72)
    print("2. THE QUEUE -- state is persisted; nothing is blocking")
    for p in agent.store.pending():
        print(f"   [{p['approval_id']}] {p['capability']:14} {p['question'][:50]}")

    print()
    print("=" * 72)
    print("3. HUMAN DECIDES -- graph RESUMES from where it stopped")
    out = agent.resume(started["thread_id"], APPROVE_WITH_EDITS, "erjon",
                       reason="scoped to in-network facilities",
                       edited_proposal="Reviewed brief: worst HF readmission, in-network only")
    rec = out["approval_record"]
    print(f"   decision:  {rec['decision']} by {rec['decided_by']}")
    print(f"   queued:    {rec['queue_seconds']:.2f}s")
    print(f"   committed: {out['committed']}")
    print(f"   final:     {out['proposal']}")

    print()
    print("   trajectory:")
    for s in out["trajectory"]:
        print(f"     {s}")

    print()
    print("=" * 72)
    print("4. REJECTION PATH -- approved actions execute, rejected ones do not")
    s2 = agent.start("Which hospitals have the longest psychiatric ED waits?")
    out2 = agent.resume(s2["thread_id"], REJECT, "erjon",
                        reason="needs clinical review before outreach")
    print(f"   committed: {out2['committed']}  (reason: {out2['approval_record']['reason']})")

    print()
    print("=" * 72)
    print("5. ESCALATION PATH -- a HANDOFF, not a verdict")
    s3 = agent.start("Which hospitals have the worst sepsis mortality?")
    out3 = agent.resume(s3["thread_id"], ESCALATE, "nurse.a",
                        reason="above my level", escalated_to="dr.b")
    print(f"   status:    {out3['status']}  (committed: {out3['committed']})")
    print(f"   owned by:  {out3['assigned_to']}")
    print("   still in the queue:")
    for p in agent.store.pending():
        print(f"     [{p['approval_id']}] assigned to {p['assigned_to']} -- {p['question'][:44]}")

    print()
    print("   ...and the new owner can still make it happen:")
    out4 = agent.resume(s3["thread_id"], APPROVE, "dr.b", reason="reviewed, proceed")
    print(f"   committed: {out4['committed']}")
    aid = agent.store.find_by_thread(s3["thread_id"])
    print("   decision chain:")
    for e in agent.store.history(aid):
        arrow = f" -> {e['escalated_to']}" if e["escalated_to"] else ""
        print(f"     {e['decision']:10} by {e['decided_by']}{arrow}")

    print()
    print("=" * 72)
    print("6. APPROVAL METRICS")
    import json
    print(json.dumps(agent.store.metrics(), indent=2))


if __name__ == "__main__":
    _demo()
