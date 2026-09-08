"""
app.py  --  Security-Constrained Agent Runtime, HTTP service layer
==================================================================
Turns the governed runtime into a callable service. A question (or raw SQL)
comes in over HTTP; a governed answer plus the full decision trail goes out.

The governance is part of the API contract, not an internal detail: every
response says whether the request was allowed, which layer decided, the reason,
and the exact safe SQL that ran. That is the product -- a boundary a buyer can
put an LLM behind without it touching anything its guardrails forbid.

Endpoints:
  GET  /health      service + dependency status
  GET  /schema      the Gold schema the agent may query
  POST /query       { "question": "..." }  natural language -> governed answer
  POST /raw-sql     { "sql": "SELECT ..." } guarded SQL execution

Run locally:
    pip install fastapi "uvicorn[standard]"
    uvicorn app:app --reload --port 8000
Then open http://localhost:8000/docs for the interactive API.
"""

from __future__ import annotations

import os
import sys
import hmac
import json
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Security, status
from fastapi.security import APIKeyHeader
from fastapi import Request
from fastapi.responses import FileResponse, RedirectResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from pydantic import BaseModel, Field

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Load a local .env (if present) BEFORE anything reads os.environ, so API_KEY,
# rate-limit settings, and planner provider/keys can come from .env in dev. This
# runs before the project imports below, several of which read env at import time
# (e.g. the planner's ACTIVE_PROVIDER). Optional dependency: never hard-fail if
# python-dotenv is not installed.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from src.runtime.agent_runtime import AgentRuntime
from analytics_query_tool import AnalyticsQueryTool
from nl_to_sql_planner import (NLToSQLPlanner, SCHEMA_DOC,
                               SCHEMA_DOC_HOSPITAL, SCHEMA_DOC_FHIR)
import cost_tracker
import schema_check
from approval import (ApprovalStore, APPROVE, REJECT, ESCALATE,
                      APPROVE_WITH_EDITS, DECISIONS)

GOLD_DB = os.environ.get("HOSPITAL_GOLD_DB", "medallion/hospital_gold.duckdb")
POLICY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "medicare_policy.yaml")
CAPABILITY = "analytics.query_aggregate"
_REFRESH_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "medallion", "REFRESH.json")

# Datasets the service can answer against. Each pairs a Gold database with the
# schema doc the planner must use for it -- they MUST stay in sync (a schema doc
# describing tables the database lacks makes every query fail silently).
FHIR_GOLD_DB = os.environ.get("FHIR_GOLD_DB", "medallion/fhir_gold.duckdb")
DATASETS = {
    "hospital": {"db": GOLD_DB, "schema_doc": SCHEMA_DOC_HOSPITAL,
                 "label": "Hospital quality (CMS)"},
    "fhir": {"db": FHIR_GOLD_DB, "schema_doc": SCHEMA_DOC_FHIR,
             "label": "Clinical records (FHIR)"},
}
DEFAULT_DATASET = os.environ.get("PLANNER_SCHEMA", "hospital").lower()


def _dataset(name: Optional[str]) -> str:
    n = (name or DEFAULT_DATASET).lower()
    return n if n in DATASETS and os.path.exists(DATASETS[n]["db"]) else DEFAULT_DATASET

# Human-readable provenance for the "What's inside" view. Descriptive only.
_DATASET_SOURCES = {
    "hospital": {
        "name": "CMS Hospital Quality",
        "description": "U.S. hospitals across 12 Northeast/Mid-Atlantic states: overall star rating, "
                       "Medicare spending per beneficiary, 30-day readmission rates (5 conditions), "
                       "and ED-flow measures including psychiatric ED boarding time.",
        "origin": "Public CMS Care Compare / Provider Data Catalog (data.cms.gov)",
        "phi": "Public, facility-level data. No PHI.",
    },
    "fhir": {
        "name": "FHIR Clinical (Synthea)",
        "description": "1,180 synthetic patients (~367k FHIR R4 resources) flattened into gold "
                       "patient / encounter / condition / observation / medication / procedure tables.",
        "origin": "Synthea synthetic patient generator (Sep 2019 sample).",
        "phi": "Synthetic patients only. No real PHI.",
    },
}

# --- API key auth -----------------------------------------------------------
# Set API_KEY in the environment to require a key on the data endpoints.
# If unset, the service runs in OPEN dev mode (flagged loudly in /health and at
# startup). Never deploy publicly without API_KEY set.
API_KEY = os.environ.get("API_KEY")

# FAIL CLOSED. Running without authentication must be an AFFIRMATIVE act, never
# the consequence of a missing variable. A misconfiguration should stop the
# service, not silently disable its access control.
#
# The escape hatch exists because local development needs to be frictionless --
# this is the pattern real systems use (DEBUG=true, ALLOW_INSECURE=1). The
# property that matters is that disabling security requires someone to SAY SO.
ALLOW_OPEN_ACCESS = os.environ.get("ALLOW_OPEN_ACCESS", "").lower() in {"1", "true", "yes"}
if not API_KEY and not ALLOW_OPEN_ACCESS:
    raise SystemExit(
        "\nREFUSING TO START: no API_KEY set.\n"
        "  The data endpoints would be publicly readable.\n"
        "  Set API_KEY=<secret> to require authentication, or set\n"
        "  ALLOW_OPEN_ACCESS=true to explicitly run without it (local dev only).\n"
    )

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

# RATE LIMITING.
# In production this belongs at the EDGE -- API gateway, load balancer, Cloudflare
# -- so abusive traffic is rejected before it consumes application resources.
# It is implemented here because this deployment has no gateway in front of it,
# and because the data endpoints call an LLM on every request, so an unthrottled
# public endpoint is a real cost exposure, not a theoretical one.
#
# The limiter is keyed per API KEY (not per IP): the key is the identity that
# actually maps to a quota, and IPs are shared behind NAT and proxies.
# storage_uri defaults to in-memory; point it at Redis for multi-instance.
RATE_LIMIT_QUERY = os.environ.get("RATE_LIMIT_QUERY", "20/minute")   # LLM-backed
RATE_LIMIT_READ = os.environ.get("RATE_LIMIT_READ", "60/minute")     # cheap reads
RATE_LIMIT_STORAGE = os.environ.get("RATE_LIMIT_STORAGE", "memory://")


def _rate_limit_key(request: Request) -> str:
    """Identity for the limiter: the API key when present, else the client IP."""
    key = request.headers.get("X-API-Key")
    if key:
        return f"key:{key[:12]}"          # truncated: never log a full secret
    client = request.client
    return f"ip:{client.host if client else 'unknown'}"


limiter = Limiter(key_func=_rate_limit_key, storage_uri=RATE_LIMIT_STORAGE)


def require_api_key(provided: Optional[str] = Security(_api_key_header)) -> None:
    if not API_KEY:
        return  # dev mode: no key configured, allow (see /health auth_enabled)
    if provided is None or not hmac.compare_digest(provided, API_KEY):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing API key (send header X-API-Key)",
        )

# Built once at startup, shared across requests. DuckDB connections are not
# thread-safe, and FastAPI runs sync endpoints in a threadpool, so we serialize
# tool execution with a lock. Correct and sufficient for a single-instance
# free-tier deploy; horizontal scaling is a later concern.
_STATE: dict[str, Any] = {"runtime": None, "planner": None, "planner_error": None}
_LOCK = threading.Lock()


@asynccontextmanager
async def lifespan(app: FastAPI):
    runtime = AgentRuntime()
    runtime.load_policy(POLICY_PATH)
    _analytics = AnalyticsQueryTool(
        db_path=GOLD_DB, seed_demo=False,
        db_paths={k: v["db"] for k, v in DATASETS.items()})
    runtime.register_tool(_analytics)
    _STATE["runtime"] = runtime
    _STATE["analytics_tool"] = _analytics
    if not API_KEY:
        print("WARNING: API_KEY not set -- data endpoints are OPEN (dev mode). "
              "Set API_KEY before exposing this service publicly.")
    # planner is optional: /raw-sql works without an LLM key, /query needs one
    try:
        _STATE["planner"] = NLToSQLPlanner()
    except BaseException as e:  # SystemExit if no API key
        _STATE["planner"] = None
        _STATE["planner_error"] = str(e)
    # Approval agent is optional: /query and /raw-sql work without it. It needs
    # the planner (to draft) and the checkpointer (to persist the pause).
    try:
        from approval_graph import GovernedApprovalAgent
        _STATE["approval_agent"] = GovernedApprovalAgent(
            db_path=GOLD_DB, runtime=runtime, planner=_STATE.get("planner"))
        _STATE["approval_store"] = _STATE["approval_agent"].store
    except BaseException as e:
        _STATE["approval_agent"] = None
        _STATE["approval_store"] = ApprovalStore()   # queue is still readable
        _STATE["approval_error"] = str(e)
    yield
    _STATE.clear()


app = FastAPI(
    title="Security-Constrained Agent Runtime",
    description="Governed natural-language analytics over CMS Medicare data.",
    version="1.0.0",
    lifespan=lifespan,
)

# wire the limiter: state, the 429 handler, and the middleware that enforces it
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)


# ---------------------------------------------------------------------------
# Minimal web UI: a self-contained page (web/index.html) that calls /query and
# /raw-sql with the same X-API-Key auth. Unauthenticated static page; the data
# endpoints it calls stay guarded.
# ---------------------------------------------------------------------------
_WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


@app.get("/", include_in_schema=False)
def _root():
    return RedirectResponse(url="/ui")


@app.get("/ui", include_in_schema=False)
def _ui():
    # no-store: the UI is a single file that changes often, and a stale cached copy
    # silently strips new behaviour (buttons that do nothing) while looking fine.
    # Always serve the current page.
    return FileResponse(
        os.path.join(_WEB_DIR, "index.html"),
        media_type="text/html",
        headers={"Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache"},
    )


# ---------------------------------------------------------------------------
# Request / response models -- the contract.
# ---------------------------------------------------------------------------
class QueryRequest(BaseModel):
    question: str = Field(..., examples=["Which hospitals give the best value, high quality and low cost?"])
    backend: Optional[str] = Field(None, description="data backend: local (default) | databricks")
    dataset: Optional[str] = Field(None, description="dataset: hospital | fhir")


class SqlRequest(BaseModel):
    sql: str = Field(..., examples=["SELECT facility_name, star_rating FROM gold_hospital_profile WHERE star_rating = 5 LIMIT 10"])
    backend: Optional[str] = Field(None, description="data backend: local (default) | databricks")
    dataset: Optional[str] = Field(None, description="dataset: hospital | fhir")


class ProposeRequest(BaseModel):
    question: str = Field(..., examples=["Which hospitals have the worst heart failure readmission rates?"])
    capability: str = Field("brief.commit", examples=["brief.commit"])


class DecisionRequest(BaseModel):
    """A reviewer's verdict on a pending proposal."""
    decision: str = Field(..., examples=["approve"],
                          description="approve | reject | escalate | approve_with_edits")
    decided_by: str = Field(..., examples=["e.brucaj"])
    reason: Optional[str] = None
    edited_proposal: Optional[str] = Field(
        None, description="required when decision is approve_with_edits")
    escalated_to: Optional[str] = Field(
        None, description="required when decision is escalate")


class GovernedResponse(BaseModel):
    allowed: bool
    decided_by: str                      # "policy" | "guard" | "executed"
    reason: Optional[str] = None
    sql: Optional[str] = None            # the SQL that was evaluated
    safe_sql: Optional[str] = None       # what the guard actually ran
    row_count: Optional[int] = None
    columns: Optional[list] = None
    rows: Optional[list] = None
    planner_metrics: Optional[dict] = None   # per-call latency, tokens, est cost
    backend: Optional[str] = None            # which data backend served the query
    dataset: Optional[str] = None            # which dataset answered
    unanswerable: Optional[str] = None       # set when the dataset cannot answer
    suggest_dataset: Optional[str] = None    # a dataset that might answer instead


# ---------------------------------------------------------------------------
# Core: run SQL through the governed runtime and map the result to the contract.
# Pure-ish mapping factored out so it is unit-testable without the runtime.
# ---------------------------------------------------------------------------
def interpret_result(result: Any, sql: str) -> dict:
    # policy/guard denial both surface as allowed=False (single denial channel);
    # distinguish by whether the guard named itself in the explanation.
    if not getattr(result, "allowed", False):
        reason = getattr(result, "explanation", None)
        if not reason and getattr(result, "decision", None) is not None:
            reason = getattr(result.decision, "reason", None)
        reason = reason or "not permitted"
        decided_by = "guard" if "sql_guard" in reason or "guard" in reason.lower() else "policy"
        return {"allowed": False, "decided_by": decided_by, "reason": reason, "sql": sql}

    tr = getattr(result, "result", None)
    if tr is None:
        return {"allowed": True, "decided_by": "policy", "reason": "allowed, no tool result", "sql": sql}
    if not getattr(tr, "success", False):
        return {"allowed": False, "decided_by": "guard", "reason": getattr(tr, "error", "denied by guard"), "sql": sql}

    out = tr.output or {}
    return {
        "allowed": True,
        "decided_by": "executed",
        "reason": None,
        "sql": sql,
        "safe_sql": out.get("safe_sql"),
        "row_count": out.get("row_count"),
        "columns": out.get("columns"),
        "rows": out.get("rows"),
        "backend": out.get("backend"),
        "dataset": out.get("dataset"),
    }


def _unanswerable(out: dict) -> Optional[str]:
    """Detect a planner refusal dressed up as a result row.

    When a question cannot be answered from the active dataset the model returns
    a single explanatory string (ideally `... AS unanswerable`). Surfacing that as
    a normal answer is misleading -- it looks like the question WAS answered -- so
    callers get an explicit signal instead.
    """
    cols, rows = out.get("columns") or [], out.get("rows") or []
    if len(cols) != 1 or len(rows) != 1:
        return None
    name = str(cols[0]).lower()
    value = rows[0].get(cols[0])
    if not isinstance(value, str):
        return None
    if "unanswerable" in name or "error" in name or "message" in name:
        return value
    return None


def run_governed_sql(sql: str, backend: Optional[str] = None,
                     dataset: Optional[str] = None) -> dict:
    runtime = _STATE["runtime"]
    params: dict[str, Any] = {"sql": sql}
    if backend:
        params["backend"] = backend
    if dataset:
        params["dataset"] = dataset
    with _LOCK:
        result = runtime.execute_tool(CAPABILITY, params)
    return interpret_result(result, sql)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "gold_db": GOLD_DB,
        "gold_db_present": os.path.exists(GOLD_DB),
        "runtime_ready": _STATE.get("runtime") is not None,
        "planner_enabled": _STATE.get("planner") is not None,
        "planner_error": _STATE.get("planner_error"),
        "auth_enabled": bool(API_KEY),
        "open_access_override": ALLOW_OPEN_ACCESS,
        "rate_limits": {"llm_endpoints": RATE_LIMIT_QUERY, "read_endpoints": RATE_LIMIT_READ},
        "approvals_enabled": _STATE.get("approval_agent") is not None,
        "approval_error": _STATE.get("approval_error"),
        "backends": _STATE["analytics_tool"].backends() if _STATE.get("analytics_tool") else None,
    }


@app.get("/ui-config", include_in_schema=False)
def ui_config(request: Request) -> dict:
    """Bootstrap config for the web UI.

    When the browser is on THIS machine (loopback), hand it the API key so local
    use needs no setup. That leaks nothing: anyone on the host can already read
    .env. Remote callers (e.g. the public deploy) never receive it and must enter
    a key, so the data endpoints stay protected.
    """
    client = request.client
    is_local = bool(client) and client.host in {"127.0.0.1", "::1", "localhost"}
    return {
        "auth_required": bool(API_KEY),
        "is_local": is_local,
        "prefill_key": API_KEY if (is_local and API_KEY) else None,
        "datasets": [{"id": k, "label": v["label"],
                      "available": os.path.exists(v["db"])} for k, v in DATASETS.items()],
        "default_dataset": DEFAULT_DATASET,
    }


@app.get("/auth-check", dependencies=[Depends(require_api_key)])
def auth_check() -> dict:
    """Cheap authenticated ping so the UI can validate a key without running a
    query. 200 = the key works; 401 = it does not."""
    return {"ok": True, "auth_enabled": bool(API_KEY)}


@app.get("/schema")
def schema(dataset: Optional[str] = None) -> dict:
    ds = _dataset(dataset)
    return {"capability": CAPABILITY, "dataset": ds, "schema": DATASETS[ds]["schema_doc"]}


@app.get("/datasets")
def datasets(dataset: Optional[str] = None) -> dict:
    """What the agent is pulling from: the active Gold dataset, its tables + row
    counts, when it was last built/refreshed, and monthly-refresh status. Powers
    the 'What's inside' view. Read-only, no auth (like /health).

    Table names come from the schema doc (the guard blocks information_schema);
    row counts run THROUGH the guard (run_governed_sql), so nothing bypasses it.
    """
    active = _dataset(dataset)
    db = DATASETS[active]["db"]
    info: dict[str, Any] = {
        "active_dataset": active,
        "available_datasets": [{"id": k, "label": v["label"],
                                "available": os.path.exists(v["db"])}
                               for k, v in DATASETS.items()],
        "source": _DATASET_SOURCES.get(active, {}),
        "gold_db": db,
        "gold_db_present": os.path.exists(db),
        "last_built": None,
        "tables": [],
        "refresh": None,
        "backends": _STATE["analytics_tool"].backends() if _STATE.get("analytics_tool") else None,
    }
    if os.path.exists(db):
        ts = os.path.getmtime(db)
        info["last_built"] = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    for t in sorted(schema_check.tables_in_schema_doc(DATASETS[active]["schema_doc"])):
        res = run_governed_sql(f"SELECT count(*) AS n FROM {t}", dataset=active)
        rows = res.get("rows") or []
        info["tables"].append({"table": t, "rows": rows[0].get("n") if rows else None})
    if os.path.exists(_REFRESH_PATH):
        try:
            with open(_REFRESH_PATH) as f:
                info["refresh"] = json.load(f)
        except (OSError, ValueError):
            info["refresh"] = None
    return info


@app.post("/raw-sql", response_model=GovernedResponse, dependencies=[Depends(require_api_key)])
@limiter.limit(RATE_LIMIT_READ)
def raw_sql(request: Request, req: SqlRequest) -> dict:
    return run_governed_sql(req.sql, req.backend, _dataset(req.dataset))


@app.post("/query", response_model=GovernedResponse, dependencies=[Depends(require_api_key)])
@limiter.limit(RATE_LIMIT_QUERY)
def query(request: Request, req: QueryRequest) -> dict:
    planner = _STATE.get("planner")
    if planner is None:
        return {
            "allowed": False,
            "decided_by": "policy",
            "reason": f"planner not configured: {_STATE.get('planner_error')}",
        }
    try:
        ds = _dataset(req.dataset)
        sql, metrics = planner.generate_sql_with_metrics(
            req.question, schema_doc=DATASETS[ds]["schema_doc"])
    except Exception as e:
        return {"allowed": False, "decided_by": "policy", "reason": f"planner error: {e}"}
    out = run_governed_sql(sql, req.backend, ds)
    note = _unanswerable(out)
    if note:
        out["unanswerable"] = note
        other = next((k for k in DATASETS
                      if k != ds and os.path.exists(DATASETS[k]["db"])), None)
        out["suggest_dataset"] = other
        out["rows"], out["columns"], out["row_count"] = [], [], 0
    out["planner_metrics"] = metrics.as_dict()
    cost_tracker.record(metrics.as_dict(), question=req.question)
    return out


@app.get("/metrics")
def metrics() -> dict:
    """Aggregate cost and latency across all planner calls (optimization view)."""
    return cost_tracker.summarize()


# ---------------------------------------------------------------------------
# Human-in-the-loop approval.
#
# /propose runs the agent to the checkpoint and RETURNS IMMEDIATELY with a
# pending id -- the graph state is persisted, not held open on this request.
# A reviewer later GETs the queue and POSTs a decision, which RESUMES the graph
# from exactly where it stopped. That is what makes this an approval QUEUE
# rather than a blocking call.
# ---------------------------------------------------------------------------

@app.post("/propose", dependencies=[Depends(require_api_key)])
@limiter.limit(RATE_LIMIT_QUERY)
def propose(request: Request, req: ProposeRequest) -> dict:
    """Run the agent until it drafts a consequential action, then pause."""
    agent = _STATE.get("approval_agent")
    if agent is None:
        raise HTTPException(status_code=503,
                            detail=f"approvals unavailable: {_STATE.get('approval_error')}")
    with _LOCK:
        return agent.start(req.question)


@app.get("/approvals", dependencies=[Depends(require_api_key)])
@limiter.limit(RATE_LIMIT_READ)
def list_approvals(request: Request) -> dict:
    """The review queue: everything awaiting a human decision."""
    store = _STATE.get("approval_store") or ApprovalStore()
    return {"pending": store.pending(), "metrics": store.metrics()}


@app.get("/approvals/{approval_id}", dependencies=[Depends(require_api_key)])
@limiter.limit(RATE_LIMIT_READ)
def get_approval(request: Request, approval_id: str) -> dict:
    store = _STATE.get("approval_store") or ApprovalStore()
    item = store.get(approval_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"no approval {approval_id}")
    # The columns hold only the FINAL verdict. An item escalated twice before it
    # was approved has one row and three decisions, so the chain has to come
    # with it or the audit trail is only reachable from Python.
    return {**item, "history": store.history(approval_id)}


@app.post("/approvals/{approval_id}", dependencies=[Depends(require_api_key)])
@limiter.limit(RATE_LIMIT_READ)
def decide_approval(request: Request, approval_id: str, req: DecisionRequest) -> dict:
    """Submit a decision.

    approve / approve_with_edits -> resumes the graph; the action executes
    reject                       -> resumes the graph; the action does not run
    escalate                     -> does NOT resume. The item is reassigned to
                                    `escalated_to`, stays pending, and stays in
                                    the queue with the thread still paused, so
                                    the new owner can still approve it and have
                                    the action actually run.
    """
    agent = _STATE.get("approval_agent")
    store = _STATE.get("approval_store") or ApprovalStore()
    if req.decision not in DECISIONS:
        raise HTTPException(status_code=400,
                            detail=f"decision must be one of {sorted(DECISIONS)}")
    item = store.get(approval_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"no approval {approval_id}")
    if item["status"] != "pending":
        raise HTTPException(status_code=409,
                            detail=f"approval {approval_id} already {item['status']}")
    if agent is None:
        raise HTTPException(status_code=503,
                            detail=f"approvals unavailable: {_STATE.get('approval_error')}")
    try:
        with _LOCK:
            return agent.resume(
                thread_id=item["thread_id"], decision=req.decision,
                decided_by=req.decided_by, reason=req.reason,
                edited_proposal=req.edited_proposal, escalated_to=req.escalated_to)
    except (ValueError, KeyError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/")
def root() -> dict:
    return {
        "service": "Security-Constrained Agent Runtime",
        "docs": "/docs",
        "endpoints": ["/health", "/schema", "/query", "/raw-sql", "/metrics",
                      "/propose", "/approvals", "/approvals/{id}"],
    }
