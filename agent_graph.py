"""
agent_graph.py
==============
The agent as a STATEFUL GRAPH (LangGraph), not a single-shot call.

Why this exists: the original planner is one-shot. Question in, SQL out, and if
the guard denies it or the SQL errors, that is the end -- the agent has no way to
recover. Real agentic systems recover: they read the failure, revise, and try
again within bounds.

The graph:

    plan ---> critic ---> execute ---> route --(success)--> finish
     ^          |                        |
     |          +--(revise, w/ reason)---+
     +------------(retry w/ reason)------+   (bounded; gives up gracefully when exhausted)

  The critic (critic.py, PLAN v2 item 4) reviews each proposed SQL before it
  runs. "revise" re-plans with its reason and costs an attempt; "approve" and
  "unavailable" go on to execute, where the guard decides as it always has. The
  critic can only make the agent stricter -- it never informs or skips the guard.
  CRITIC_ENABLED=0 removes the node; CRITIC_ROUTING=0 keeps it as a recorder.

Design decisions worth defending:

  SELF-CORRECTION. When execute fails, the FAILURE REASON is fed back into the
  next plan attempt. The agent sees "DENIED by sql_guard: table not on Gold
  allowlist: billing_raw" and writes a different query. This is the core
  capability upgrade over the one-shot planner.

  BOUNDED RETRIES. MAX_ATTEMPTS caps the loop. An unbounded agent loop is a real
  production failure mode: cost and latency runaway, and a model that keeps
  making the same mistake forever. Bounded retry with graceful give-up is the
  correct shape.

  GOVERNANCE IS UNCHANGED. The graph decides WHAT TO TRY NEXT. sql_guard and the
  policy engine still decide WHAT IS ALLOWED TO RUN. LangGraph orchestrates
  around the governance; it does not replace or weaken it. Every attempt --
  including every retry -- goes through the same runtime.

  TRAJECTORY CAPTURE. Every step is recorded in state["trajectory"]: what was
  attempted, what the guard said, how long it took, what it cost. You cannot
  evaluate a path you did not record, and trajectory-level evaluation (did the
  agent take sane steps, not just land on the answer) is what the agentic-AI job
  descriptions ask for.

  ALONGSIDE, NOT REPLACING. nl_to_sql_planner.py is untouched and still works.
  This graph USES it as a component. That means the existing eval harness, API,
  and validator keep running, and you can A/B the two paths against the eval
  suite to MEASURE whether self-correction actually improves accuracy.

Run the demo (needs the planner key + the hospital Gold):
    python agent_graph.py
"""

import contextlib
import os
import sys
from typing import Any, Optional, TypedDict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Load a local .env (if present) before reading os.environ. Optional dependency.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.sqlite import SqliteSaver

import critic
import tracing
from src.runtime.agent_runtime import AgentRuntime
from analytics_query_tool import AnalyticsQueryTool
from nl_to_sql_planner import NLToSQLPlanner, SCHEMA_DOC
import schema_check

GOLD_DB = os.environ.get("HOSPITAL_GOLD_DB", "medallion/hospital_gold.duckdb")
POLICY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "medicare_policy.yaml")
CHECKPOINT_DB = os.environ.get("CHECKPOINT_DB", "logs/checkpoints.sqlite")
CAPABILITY = "analytics.query_aggregate"

MAX_ATTEMPTS = 3          # bounded retries: cost/latency runaway is a real failure mode


_METRIC_FIELDS = ("latency_ms", "prompt_tokens", "completion_tokens", "total_tokens",
                  "est_cost_usd", "model", "provider")


def _metrics_dict(m: Any) -> dict:
    """CallMetrics.as_dict(), or the same fields read off any metrics-like object."""
    if hasattr(m, "as_dict"):
        return m.as_dict()
    return {k: getattr(m, k, None) for k in _METRIC_FIELDS}


class AgentState(TypedDict, total=False):
    """State carried through the graph. Every node reads and updates this."""
    question: str
    trace_id: str                   # names the trace AND the checkpoint thread
    dataset: Optional[str]          # which Gold to plan and run against (the API's datasets)
    backend: Optional[str]          # local | databricks (the API's backend selector)
    backend_used: Optional[str]     # the backend that actually ran the query
    critic_verdict: Optional[str]   # approve | revise | unavailable, for the latest plan
    sql: Optional[str]              # the SQL from the latest plan step
    attempts: int                   # how many plan->execute cycles so far
    last_error: Optional[str]       # why the previous attempt failed (fed back into plan)
    allowed: bool
    decided_by: Optional[str]       # "executed" | "guard" | "policy" | "critic" | "exhausted"
    reason: Optional[str]
    safe_sql: Optional[str]
    row_count: Optional[int]
    columns: Optional[list]
    rows: Optional[list]
    trajectory: list                # every step, in order -- the agent's PATH
    total_latency_ms: int
    total_tokens: int
    total_cost_usd: float


class GovernedAgentGraph:
    """Builds and runs the stateful agent graph over the governed runtime."""

    def __init__(self, db_path: str = GOLD_DB, max_attempts: int = MAX_ATTEMPTS,
                 provider: Optional[str] = None, runtime: Optional[AgentRuntime] = None,
                 planner: Any = None, checkpoint_db: Optional[str] = None,
                 tracer: Optional[tracing.Tracer] = None,
                 schema_docs: Optional[dict] = None, lock: Any = None):
        """`schema_docs` maps a run's `dataset` to the schema the planner and
        critic see (the API serves several datasets from one graph); without it
        the planner's default schema is used. `lock` serialises execute_tool
        with the caller's other users of the same runtime (the API's _LOCK)."""
        self.max_attempts = max_attempts
        self.schema_docs = schema_docs or {}
        self._lock = lock
        self.critic_on = critic.enabled()
        self.critic_routes = critic.routing()
        if runtime is not None:
            # injected (tests, the API): the caller owns the tool registration
            self.runtime = runtime
            tool = None
        else:
            self.runtime = AgentRuntime()
            self.runtime.load_policy(POLICY_PATH)
            tool = AnalyticsQueryTool(db_path=db_path, seed_demo=False)
            self.runtime.register_tool(tool)
        if planner is not None:
            self.planner = planner
        else:
            self.planner = NLToSQLPlanner(provider=provider) if provider else NLToSQLPlanner()
        if tool is not None:
            # Fail loudly if PLANNER_SCHEMA does not match the connected database.
            # A mismatch makes every query fail against tables that do not exist --
            # silent, misleading, and it looks like the agent is broken when the
            # configuration is.
            schema_check.verify(SCHEMA_DOC, tool._con,
                                schema_label=os.environ.get("PLANNER_SCHEMA", "hospital"),
                                db_label=db_path)
        self.tracer = tracer or tracing.get_tracer()
        # The checkpointer is what lets a run be inspected after the fact and
        # resumed after a failure: every node's output is saved under the
        # thread id, which is also the trace id (tracing.py).
        self.checkpoint_db = checkpoint_db or CHECKPOINT_DB
        os.makedirs(os.path.dirname(self.checkpoint_db) or ".", exist_ok=True)
        self._cm = SqliteSaver.from_conn_string(self.checkpoint_db)
        self.checkpointer = self._cm.__enter__()
        # Result rows never enter the graph state, because the checkpointer
        # writes every state to disk and nothing prunes it. They are held here
        # by trace id for the length of run() and handed back to the caller.
        self._rows: dict[str, Optional[list]] = {}
        self.graph = self._build()

    def _traced(self, state: AgentState):
        """Nodes may run on a worker thread without the request's context; attach
        the run's trace from the state so their spans land in the right place."""
        tid = state.get("trace_id")
        if tid and tracing.current_trace_id() != tid:
            return self.tracer.attach(tid)
        return contextlib.nullcontext(tid)

    # ---------------- nodes ----------------

    def _plan(self, state: AgentState) -> dict:
        """Write SQL. On a retry, include WHY the last attempt failed."""
        question = state["question"]
        attempt = state.get("attempts", 0) + 1
        last_error = state.get("last_error")

        prompt = question
        if last_error:
            # self-correction: the agent sees its own failure and revises
            prompt = (
                f"{question}\n\n"
                f"Your previous SQL attempt was REJECTED. Reason:\n{last_error}\n\n"
                f"Write a corrected query that avoids this problem. Use only the "
                f"allowed table and columns from the schema."
            )

        schema_doc = self.schema_docs.get(state.get("dataset"))
        kw = {"schema_doc": schema_doc} if schema_doc else {}
        with self._traced(state), self.tracer.span(
                "plan", attempt=attempt, retry_context=bool(last_error)) as sp:
            sql, metrics = self.planner.generate_sql_with_metrics(prompt, **kw)
            sp.set(sql=sql, sql_sha256=tracing.sql_sha256(sql), model=metrics.model,
                   provider=metrics.provider, prompt_tokens=metrics.prompt_tokens,
                   completion_tokens=metrics.completion_tokens,
                   est_cost_usd=metrics.est_cost_usd, latency_ms=metrics.latency_ms)
        step = {
            "step": "plan",
            "attempt": attempt,
            "sql": sql,
            "retry_context": bool(last_error),
            "latency_ms": metrics.latency_ms,
            "tokens": metrics.total_tokens,
            "est_cost_usd": metrics.est_cost_usd,
            "metrics": _metrics_dict(metrics),
        }
        return {
            "sql": sql,
            "critic_verdict": None,
            "attempts": attempt,
            "trajectory": state.get("trajectory", []) + [step],
            "total_latency_ms": state.get("total_latency_ms", 0) + metrics.latency_ms,
            "total_tokens": state.get("total_tokens", 0) + metrics.total_tokens,
            "total_cost_usd": round(state.get("total_cost_usd", 0.0) + metrics.est_cost_usd, 6),
        }

    def _critic(self, state: AgentState) -> dict:
        """Review the proposed SQL. Records the verdict; never touches the guard."""
        sql = state.get("sql") or ""
        attempt = state.get("attempts")
        with self._traced(state), self.tracer.span(
                "critic", attempt=attempt, sql_sha256=tracing.sql_sha256(sql),
                routing=self.critic_routes) as sp:
            v = critic.review(self.planner, state["question"], sql,
                              schema_doc=self.schema_docs.get(state.get("dataset")))
            m = v.get("metrics") or {}
            sp.set(verdict=v["verdict"], reason=v["reason"][:500], issues=v["issues"],
                   model=m.get("model"), provider=m.get("provider"),
                   prompt_tokens=m.get("prompt_tokens"), completion_tokens=m.get("completion_tokens"),
                   est_cost_usd=m.get("est_cost_usd"), latency_ms=m.get("latency_ms"))
        critic.audit(self.runtime, CAPABILITY, v, tracing.sql_sha256(sql))
        step = {"step": "critic", "attempt": attempt, "verdict": v["verdict"],
                "reason": v["reason"][:200], "tokens": m.get("total_tokens", 0),
                "est_cost_usd": m.get("est_cost_usd", 0.0), "metrics": m or None}
        out = {
            "critic_verdict": v["verdict"],
            "trajectory": state.get("trajectory", []) + [step],
            "total_latency_ms": state.get("total_latency_ms", 0) + m.get("latency_ms", 0),
            "total_tokens": state.get("total_tokens", 0) + m.get("total_tokens", 0),
            "total_cost_usd": round(state.get("total_cost_usd", 0.0) + m.get("est_cost_usd", 0.0), 6),
        }
        if v["verdict"] == critic.REVISE and self.critic_routes:
            reason = f"critic: {v['reason'] or 'the SQL does not answer the question'}"
            out.update(allowed=False, decided_by="critic", reason=reason, last_error=reason)
        return out

    def _execute(self, state: AgentState) -> dict:
        """Run the SQL through the SAME governed runtime. Guard is unchanged."""
        sql = state.get("sql") or ""
        params: dict = {"sql": sql}
        for k in ("dataset", "backend"):
            if state.get(k):
                params[k] = state[k]
        with self._traced(state), self.tracer.span(
                "guard", attempt=state.get("attempts"), sql_sha256=tracing.sql_sha256(sql)) as g:
            with (self._lock or contextlib.nullcontext()):
                result = self.runtime.execute_tool(CAPABILITY, params)

            # policy-level denial
            if not getattr(result, "allowed", False):
                reason = getattr(result, "explanation", None) or "not permitted"
                decided_by = "guard" if "guard" in reason.lower() else "policy"
                g.set(allowed=False, decided_by=decided_by, reason=reason[:500])
                step = {"step": "execute", "attempt": state.get("attempts"),
                        "outcome": "denied", "decided_by": decided_by, "reason": reason[:200]}
                return {
                    "allowed": False, "decided_by": decided_by, "reason": reason,
                    "last_error": reason,
                    "trajectory": state.get("trajectory", []) + [step],
                }

            tool_result = result.result
            if tool_result is None or not getattr(tool_result, "success", False):
                err = getattr(tool_result, "error", "no tool result") if tool_result else "no tool result"
                decided_by = "guard" if "guard" in (err or "").lower() else "sql_error"
                g.set(allowed=False, decided_by=decided_by, reason=(err or "")[:500])
                step = {"step": "execute", "attempt": state.get("attempts"),
                        "outcome": "failed", "decided_by": decided_by, "reason": err[:200]}
                return {
                    "allowed": False, "decided_by": decided_by, "reason": err,
                    "last_error": err,
                    "trajectory": state.get("trajectory", []) + [step],
                }

            out = tool_result.output or {}
            self._rows[state.get("trace_id") or ""] = out.get("rows")
            g.set(allowed=True, decided_by="executed")
            self.tracer.event("execute", attempt=state.get("attempts"),
                              row_count=out.get("row_count"), backend=out.get("backend"))
        step = {"step": "execute", "attempt": state.get("attempts"),
                "outcome": "success", "row_count": out.get("row_count")}
        return {
            "allowed": True, "decided_by": "executed", "reason": None, "last_error": None,
            "safe_sql": out.get("safe_sql"), "row_count": out.get("row_count"),
            "columns": out.get("columns"), "backend_used": out.get("backend"),
            "trajectory": state.get("trajectory", []) + [step],
        }

    def _finish(self, state: AgentState) -> dict:
        """Terminal node. If we exhausted retries, say so explicitly."""
        exhausted = not state.get("allowed") and state.get("attempts", 0) >= self.max_attempts
        with self._traced(state):
            self.tracer.event("answer", outcome="exhausted" if exhausted
                              else ("answered" if state.get("allowed") else "failed"),
                              attempts=state.get("attempts"), decided_by=state.get("decided_by"),
                              row_count=state.get("row_count"))
        if exhausted:
            step = {"step": "finish", "outcome": "exhausted",
                    "attempts": state.get("attempts")}
            return {
                "decided_by": "exhausted",
                "reason": f"gave up after {state.get('attempts')} attempts: {state.get('reason')}",
                "trajectory": state.get("trajectory", []) + [step],
            }
        step = {"step": "finish", "outcome": "success" if state.get("allowed") else "failed"}
        return {"trajectory": state.get("trajectory", []) + [step]}

    # ---------------- routing ----------------

    def _route(self, state: AgentState) -> str:
        """Success -> finish. Failure with retries left -> plan again. Else finish."""
        if state.get("allowed"):
            return "finish"
        if state.get("attempts", 0) < self.max_attempts:
            with self._traced(state):
                self.tracer.event("retry", attempt=state.get("attempts"),
                                  reason=(state.get("last_error") or "")[:500])
            return "plan"          # self-correct: loop back with last_error in state
        return "finish"            # bounded: give up gracefully

    def _route_critic(self, state: AgentState) -> str:
        """approve / unavailable -> execute. revise -> plan, or finish if the
        attempts are spent (a revise consumes one, so the cap still bounds cost)."""
        if state.get("critic_verdict") == critic.REVISE and self.critic_routes:
            if state.get("attempts", 0) < self.max_attempts:
                with self._traced(state):
                    self.tracer.event("retry", attempt=state.get("attempts"), by="critic",
                                      reason=(state.get("last_error") or "")[:500])
                return "plan"
            return "finish"
        return "execute"

    # ---------------- graph ----------------

    def _build(self):
        g = StateGraph(AgentState)
        g.add_node("plan", self._plan)
        g.add_node("execute", self._execute)
        g.add_node("finish", self._finish)
        g.add_edge(START, "plan")
        if self.critic_on:
            g.add_node("critic", self._critic)
            g.add_edge("plan", "critic")
            g.add_conditional_edges("critic", self._route_critic,
                                    {"execute": "execute", "plan": "plan", "finish": "finish"})
        else:
            g.add_edge("plan", "execute")
        g.add_conditional_edges("execute", self._route, {"plan": "plan", "finish": "finish"})
        g.add_edge("finish", END)
        return g.compile(checkpointer=self.checkpointer)

    # ---------------- public API ----------------

    def state(self, trace_id: str) -> dict:
        """What the checkpoint says about a run: where it stopped and why."""
        snap = self.graph.get_state({"configurable": {"thread_id": trace_id}})
        values = getattr(snap, "values", None) or {}
        if not values:
            return {"trace_id": trace_id, "exists": False}
        nxt = list(getattr(snap, "next", ()) or ())
        return {
            "trace_id": trace_id, "exists": True, "finished": not nxt, "next": nxt,
            "attempts": values.get("attempts", 0), "last_error": values.get("last_error"),
            "decided_by": values.get("decided_by"), "allowed": values.get("allowed"),
            "steps": len(values.get("trajectory") or []),
        }

    def run(self, question: Optional[str] = None, trace_id: Optional[str] = None,
            dataset: Optional[str] = None, backend: Optional[str] = None) -> AgentState:
        """See _run. The caller's current trace is left as it was."""
        with self.tracer.preserved():
            return self._run(question, trace_id, dataset, backend)

    def _run(self, question: Optional[str] = None, trace_id: Optional[str] = None,
             dataset: Optional[str] = None, backend: Optional[str] = None) -> AgentState:
        """Run a question through the graph, or resume a run by its trace id.

        A new run gets a fresh trace id, which is also the checkpoint thread id.
        Passing the id of a run that stopped part-way (a node raised) continues it
        from the last checkpoint without repeating the steps that completed. The
        id of a finished run returns its saved state without running anything.
        Result rows are never checkpointed, so a finished run re-read by id, or
        a run resumed AFTER `execute` succeeded, comes back with `rows: None`
        and `rows_unavailable: True` -- never as an empty result.
        """
        tid = trace_id or tracing.new_id()
        cfg = {"configurable": {"thread_id": tid}}
        existing = self.state(tid) if trace_id else {"exists": False}
        if existing.get("exists") and existing.get("finished"):
            snap = self.graph.get_state(cfg)
            done = {**snap.values, "rows": None, "trace_id": tid}
            if done.get("allowed"):
                done["rows_unavailable"] = True
            return done
        resume = bool(existing.get("exists"))
        if resume:
            self.tracer.start_trace("run", trace_id=tid)   # no-op if it exists
            ctx = self.tracer.attach(tid)
        else:
            if question is None:
                raise ValueError("a new run needs a question")
            ctx = self.tracer.attach(self.tracer.start_trace(
                "run", trace_id=tid, question=question, mode="graph", dataset=dataset,
                max_attempts=self.max_attempts, critic=self.critic_on))
        with ctx:
            if resume:
                self.tracer.event("resume", from_node=existing.get("next"),
                                  attempts=existing.get("attempts"))
                payload = None
            else:
                payload: Any = {
                    "question": question, "trace_id": tid, "attempts": 0, "trajectory": [],
                    "dataset": dataset, "backend": backend,
                    "total_latency_ms": 0, "total_tokens": 0, "total_cost_usd": 0.0,
                }
            try:
                final = self.graph.invoke(payload, cfg)
            except BaseException as exc:
                self.tracer.end_trace(tid, status="error",
                                      error=f"{type(exc).__name__}: {exc}"[:500])
                raise
            finally:
                rows = self._rows.pop(tid, None)
            final["rows"] = rows
            if final.get("allowed") and rows is None:
                final["rows_unavailable"] = True
            self.tracer.end_trace(
                tid, status="ok", decided_by=final.get("decided_by"),
                attempts=final.get("attempts"), total_tokens=final.get("total_tokens"),
                total_cost_usd=final.get("total_cost_usd"), row_count=final.get("row_count"))
        final["trace_id"] = tid
        return final


# ---------------------------------------------------------------------------
# Demo: a normal question, and one designed to force self-correction.
# ---------------------------------------------------------------------------
def _demo():
    if not os.path.exists(GOLD_DB):
        sys.exit(f"missing {GOLD_DB} -- build the hospital Gold first")

    agent = GovernedAgentGraph()
    questions = [
        # Care-navigation / network steering: where should we route members for
        # a high-volume condition, balancing quality against cost?
        "For heart failure care coordination, which in-network hospitals should we "
        "steer members to, balancing readmission performance against cost?",
        # Behavioral-health access gap: psychiatric boarding in the ED is a real
        # provider-side operational problem and a care-coordination failure mode.
        "Which facilities have the longest psychiatric emergency department boarding "
        "times, where behavioral health patients are waiting for placement?",
        # This one reliably trips the guard. The CORRECT answer to "list every
        # table" is a query against information_schema -- which the guard denies
        # outright (no catalog/system schemas). The agent writes it faithfully
        # because it looks harmless, gets denied, and must self-correct. This
        # demonstrates the retry loop on a real denial rather than a contrived one.
        "List every table available in this database, using the information_schema catalog.",
    ]

    for q in questions:
        print("=" * 72)
        print(f"Q: {q}")
        state = agent.run(q)
        print(f"  attempts: {state.get('attempts')}  decided_by: {state.get('decided_by')}")
        print(f"  tokens: {state.get('total_tokens')}  "
              f"latency: {state.get('total_latency_ms')}ms  "
              f"cost: ${state.get('total_cost_usd')}")
        print("  trajectory:")
        for s in state.get("trajectory", []):
            if s["step"] == "plan":
                tag = " (retry, with failure context)" if s.get("retry_context") else ""
                print(f"    [plan  #{s['attempt']}]{tag} {s['sql'][:70]}...")
            elif s["step"] == "execute":
                print(f"    [exec  #{s['attempt']}] {s['outcome']}"
                      + (f" -> {s.get('decided_by')}: {str(s.get('reason'))[:60]}"
                         if s["outcome"] != "success" else f" -> {s.get('row_count')} rows"))
            else:
                print(f"    [finish] {s.get('outcome')}")
        if state.get("allowed"):
            for row in (state.get("rows") or [])[:3]:
                print(f"      {row}")
        else:
            print(f"  final reason: {str(state.get('reason'))[:160]}")
    print("=" * 72)


if __name__ == "__main__":
    _demo()
