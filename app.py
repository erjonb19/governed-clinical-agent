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
import contextlib
import threading
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
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
import auth_failures
import cost_tracker
import llm_budget
import schema_check
import tracing
import versioning
from src.runtime.audit_logger import AuditLogger
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

# /query's path (PLAN v2 D2). "graph" (default): the self-correcting planner graph
# with the critic -- several model calls per question, every run checkpointed.
# "single": one plan, one guard decision, as before.
QUERY_MODE = os.environ.get("QUERY_MODE", "graph").strip().lower()
if QUERY_MODE not in {"graph", "single"}:
    raise SystemExit(f"QUERY_MODE must be graph or single, not {QUERY_MODE!r}")
RATE_LIMIT_READ = os.environ.get("RATE_LIMIT_READ", "60/minute")     # cheap reads
AUDIT_LOG_DIR = os.environ.get("AUDIT_LOG_DIR", "logs")   # "" disables the audit log

RATE_LIMIT_STORAGE = os.environ.get("RATE_LIMIT_STORAGE", "memory://")


def _rate_limit_key(request: Request) -> str:
    """Identity for the limiter: the API key when present, else the client IP."""
    key = request.headers.get("X-API-Key")
    if key:
        return f"key:{key[:12]}"          # truncated: never log a full secret
    client = request.client
    return f"ip:{client.host if client else 'unknown'}"


limiter = Limiter(key_func=_rate_limit_key, storage_uri=RATE_LIMIT_STORAGE)


def _client_address(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def require_api_key(
    request: Request, provided: Optional[str] = Security(_api_key_header)
) -> None:
    if not API_KEY:
        return  # dev mode: no key configured, allow (see /health auth_enabled)
    client = _client_address(request)
    ok = provided is not None and hmac.compare_digest(provided, API_KEY)
    # Backlog item 35: every refused key is counted and audited per client.
    # With AUTH_FAILURE_LOCKOUT on, a client over the limit gets 429 for every
    # request until its window ends; off (the default, see auth_failures.py)
    # it is audit and counting only.
    if auth_failures.FAILURES.lockout and auth_failures.FAILURES.throttled(client):
        if not ok:
            auth_failures.FAILURES.record(client, request.url.path, "api")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="too many failed API key attempts; try again later",
            headers={"Retry-After": str(auth_failures.FAILURES.retry_after(client))},
        )
    if ok:
        return
    auth_failures.FAILURES.record(client, request.url.path, "api")
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="invalid or missing API key (send header X-API-Key)",
    )

# Built once at startup, shared across requests. FastAPI runs sync endpoints in
# a threadpool. Queries no longer share one lock (backlog item 20): the query
# tool opens a DuckDB cursor of its own per execution, so /query, /raw-sql and
# /mcp run side by side. Two limits remain:
#   _APPROVAL_LOCK  serialises the approval queue's own runs (/propose and a
#                   decision): its graph and store are not built for overlap,
#                   and they no longer block anyone else's queries.
#   _RUN_SLOTS      at most MAX_CONCURRENT_RUNS runs that call the model at
#                   once (free-tier rate limits); a request that waits longer
#                   than RUN_QUEUE_WAIT_SEC for a slot gets 429 + Retry-After.
_STATE: dict[str, Any] = {"runtime": None, "planner": None, "planner_error": None}


def _public_error(e: BaseException) -> str:
    """What a client may see of an exception (item 40): its type only. The
    message can carry file paths, provider responses or configuration; it goes
    to the server log and the trace, never into a response or /health."""
    if isinstance(e, SystemExit):
        return "SystemExit (not configured: see the server log)"
    return type(e).__name__


def _startup_failure(what: str, e: BaseException) -> str:
    print(f"WARNING: {what} unavailable ({type(e).__name__}: {e})")
    return _public_error(e)
_APPROVAL_LOCK = threading.Lock()


def _env_number(name: str, default: float, minimum: float) -> float:
    try:
        v = float(os.environ.get(name, default))
    except ValueError:
        return default
    return v if v >= minimum else default


MAX_CONCURRENT_RUNS = int(_env_number("MAX_CONCURRENT_RUNS", 4, 1))
RUN_QUEUE_WAIT_SEC = _env_number("RUN_QUEUE_WAIT_SEC", 15, 0)
RUN_RETRY_AFTER_SEC = 5
_RUN_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT_RUNS)


@contextlib.contextmanager
def _model_budget(planner: Any):
    """Reserve this run's worst case against the daily model budget (item 37,
    llm_budget.py), or 429 until the next UTC midnight. Yields a callable that
    settles the reservation to the run's reported cost."""
    from agent_graph import MAX_ATTEMPTS

    try:
        reserved = llm_budget.BUDGET.reserve(llm_budget.worst_case_usd(planner, MAX_ATTEMPTS))
    except llm_budget.BudgetExceeded as e:
        raise HTTPException(status_code=429, detail=f"{e.reason}; try again tomorrow (UTC)",
                            headers={"Retry-After": str(e.retry_after)}) from None
    settled = []
    yield lambda actual: settled.append(actual)
    if settled:
        llm_budget.BUDGET.settle(reserved, settled[-1])


def _reported_cost(out: Any) -> Optional[float]:
    m = out.get("planner_metrics") if isinstance(out, dict) else None
    v = m.get("est_cost_usd") if isinstance(m, dict) else None
    return float(v) if isinstance(v, (int, float)) else None


@contextlib.contextmanager
def _run_slot():
    """One of MAX_CONCURRENT_RUNS slots for a run that calls the model, or 429."""
    if not _RUN_SLOTS.acquire(timeout=RUN_QUEUE_WAIT_SEC):
        raise HTTPException(
            status_code=429,
            detail=f"busy: {MAX_CONCURRENT_RUNS} questions are already being answered; "
                   f"retry in {RUN_RETRY_AFTER_SEC} s",
            headers={"Retry-After": str(RUN_RETRY_AFTER_SEC)})
    try:
        yield
    finally:
        _RUN_SLOTS.release()


class _ServiceAuditLogger(AuditLogger):
    """The service's audit log records that a query ran, never what it returned.

    AgentRuntime logs the tool output after `redact_data`, which masks secrets,
    not data values -- so the rows of a FHIR query would land in the file
    verbatim. Drop them here; row_count and columns stay.
    """

    def log_tool_execution(self, capability, parameters, success, execution_time_ms,
                           result=None, error=None, resource_usage=None):
        out = result.get("output") if isinstance(result, dict) else None
        if isinstance(out, dict) and "rows" in out:
            result = {**result, "output": {k: v for k, v in out.items() if k != "rows"}}
        return super().log_tool_execution(capability, parameters, success, execution_time_ms,
                                          result, error, resource_usage)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # The runtime's audit logger was only ever attached by the demo scripts, so
    # the deployed service wrote no audit log. Buffer size 1: every event lands.
    audit = None
    if AUDIT_LOG_DIR:
        try:
            audit = _ServiceAuditLogger(log_dir=Path(AUDIT_LOG_DIR), agent_id="api",
                                max_buffer_size=1, enable_console=False)
        except Exception as e:
            print(f"WARNING: audit log disabled ({e})")
    runtime = AgentRuntime(audit_logger=audit)
    auth_failures.FAILURES.audit = audit    # refused keys go to the same audit log
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
        _STATE["planner_error"] = _startup_failure("planner", e)
    # The planner graph behind /query (PLAN v2 D2): plan -> critic -> guard ->
    # execute, with bounded self-correction, every run checkpointed under its
    # trace id. Same runtime, same guard, same lock as /raw-sql. Optional: if it
    # cannot be built, /query falls back to single-shot and /health says why.
    _STATE["graph_agent"] = None
    if QUERY_MODE == "graph" and _STATE.get("planner") is not None:
        try:
            from agent_graph import GovernedAgentGraph
            _STATE["graph_agent"] = GovernedAgentGraph(
                runtime=runtime, planner=_STATE["planner"],
                checkpoint_db=os.environ.get("CHECKPOINT_DB"),   # read now, not at import
                schema_docs={k: v["schema_doc"] for k, v in DATASETS.items()})
        except BaseException as e:
            _STATE["graph_error"] = _startup_failure("planner graph", e)
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
        _STATE["approval_error"] = _startup_failure("approvals", e)
    # The MCP server's tools over Streamable HTTP at /mcp (PLAN v2 item 5, D5),
    # behind MCP_API_KEY (falling back to API_KEY, D6). Optional: without the
    # mcp package /mcp answers 503 and /health says why. app.py already refused
    # to start without API_KEY unless ALLOW_OPEN_ACCESS, so a missing key here
    # means explicit open access, as for the rest of the API.
    async with AsyncExitStack() as stack:
        try:
            import mcp_server
            # same audit trail as /query and /raw-sql; no shared lock (each
            # execution opens its own DuckDB cursor, backlog item 20)
            mcp_server.attach_host(audit_logger=audit)
            manager, handler = mcp_server.build_http_handler(mcp_server.mcp_key())
            await stack.enter_async_context(manager.run())
            _STATE["mcp_http"] = handler
        except Exception as e:
            _STATE["mcp_http"] = None
            # /health is unauthenticated: the exception type only, no paths
            _STATE["mcp_error"] = type(e).__name__
            print(f"WARNING: MCP over HTTP disabled ({type(e).__name__}: {e})")
        yield
    if "mcp_server" in sys.modules:
        sys.modules["mcp_server"].attach_host()     # detach: no logger or lock outlives the app
    auth_failures.FAILURES.audit = None
    _STATE.clear()


# The app's semantic version lives in one file, VERSION (backlog item 5); a
# release is the tag vX.Y.Z that matches it (.github/workflows/release.yml).
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "VERSION"),
          encoding="utf-8") as _vf:
    APP_VERSION = _vf.read().strip()
API_VERSION = "v1"

app = FastAPI(
    title="Security-Constrained Agent Runtime",
    description="Governed natural-language analytics over CMS Medicare data.",
    version=APP_VERSION,
    lifespan=lifespan,
)


class _APIVersionPrefix:
    """Every endpoint answers under /v1 as well (backlog item 5).

    /v1/<path> is served by the route for /<path>: one set of routes, so the
    two can never drift, and the old paths keep working (the UI, the MCP
    clients and the E2Es use them). Every response says which API version
    answered (`API-Version: v1`). A future /v2 gets its own routes here.
    """

    PREFIX = "/" + API_VERSION

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        if path == self.PREFIX or path.startswith(self.PREFIX + "/"):
            new = path[len(self.PREFIX):] or "/"
            raw = scope.get("raw_path") or path.encode("utf-8")
            # strip the prefix from the ORIGINAL bytes, so percent-encoding in
            # the rest of the path survives for logs and anything reading raw_path
            raw = raw[len(self.PREFIX):] or b"/"
            scope = {**scope, "path": new, "raw_path": raw}

        async def send_with_version(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                headers.append((b"api-version", API_VERSION.encode()))
                message = {**message, "headers": headers}
            await send(message)
        await self.app(scope, receive, send_with_version)

# Request size limits (security audit item 34, SECURITY_STANDARDS 7.1 / 8):
# a question goes straight into a model prompt, so an unbounded one spends
# quota (or money); reviewer text is stored. Over a field limit is a 422 from
# validation; over the body limit is a 413 before anything is parsed.
MAX_QUESTION_CHARS = 2_000
MAX_SQL_CHARS = 20_000
MAX_TEXT_CHARS = 20_000      # an approval reason or an edited proposal
MAX_NAME_CHARS = 200         # names, decisions, datasets, backends
MAX_BODY_BYTES = 64 * 1024


class _BodySizeLimit:
    """Refuse a request body over MAX_BODY_BYTES with 413 (item 34), by its
    Content-Length or, for a chunked body without one, by counting the bytes
    as they arrive. Runs before routing, so nothing parses an oversized body."""

    def __init__(self, app, limit: int = MAX_BODY_BYTES):
        self.app = app
        self.limit = limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        for k, v in scope.get("headers") or []:
            if k.lower() == b"content-length":
                try:
                    too_big = int(v) > self.limit
                except ValueError:
                    too_big = True
                if too_big:
                    return await self._refuse(send)
        seen = 0
        tripped = False
        started = False

        class _TooBig(Exception):
            pass

        async def counting_receive():
            nonlocal seen, tripped
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body") or b"")
                if seen > self.limit:
                    tripped = True
                    raise _TooBig()
            return message

        async def guarded_send(message):
            # once the body is too big, whatever the app answers (FastAPI turns a
            # failed body read into a 400) is dropped: the 413 below is the answer
            nonlocal started
            if tripped:
                return
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counting_receive, guarded_send)
        except Exception:
            # _TooBig may arrive bare, wrapped in an ExceptionGroup (anyio task
            # groups), or not at all (caught by the framework); the flag is the
            # truth. Anything else, unrelated to the size, propagates.
            if not tripped:
                raise
        if tripped and not started:
            await self._refuse(send)

    async def _refuse(self, send):
        body = (b'{"detail":"request body too large (limit ' + str(self.limit).encode()
                + b' bytes)"}')
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


def inline_scripts(html: str) -> list[str]:
    """The text of each inline <script> (no src) in a page, exactly as the browser
    hashes it for a CSP. An HTML parser, not a regex: a regex over HTML misses
    forms such as `</script >` (CodeQL py/bad-tag-filter)."""
    from html.parser import HTMLParser

    class _Scripts(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=False)
            self.bodies: list[str] = []
            self._inline = False
            self._buf: list[str] = []

        def handle_starttag(self, tag, attrs):
            if tag == "script":
                self._inline = not any(k == "src" for k, _ in attrs)
                self._buf = []

        def handle_data(self, data):
            if self._inline:
                self._buf.append(data)

        def handle_endtag(self, tag):
            if tag == "script" and self._inline:
                self.bodies.append("".join(self._buf))
                self._inline = False

    p = _Scripts()
    p.feed(html)
    p.close()
    return p.bodies


class _SecurityHeaders:
    """Security headers on every response (security audit item 33, SECURITY_
    STANDARDS 9): no MIME sniffing, no referrer beyond the origin, no camera /
    microphone / location, HSTS, and no framing. The UI page also gets a Content
    Security Policy: scripts only from this origin plus the page's one inline
    script, by its SHA-256 (computed from the file served, so they cannot
    drift); no inline event handlers; no frames, objects or base-URI changes.
    /docs (Swagger UI from a CDN) gets the headers but no CSP: putting the docs
    behind a flag is its own item (53)."""

    COMMON = [
        (b"x-content-type-options", b"nosniff"),
        (b"referrer-policy", b"strict-origin-when-cross-origin"),
        (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
        (b"strict-transport-security", b"max-age=31536000; includeSubDomains"),
        (b"x-frame-options", b"DENY"),
    ]
    CSP_PATHS = ("/ui", "/v1/ui")

    def __init__(self, app):
        self.app = app
        self.csp = self.build_csp().encode()

    @staticmethod
    def build_csp() -> str:
        import base64
        import hashlib
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "index.html"),
                  encoding="utf-8") as f:
            html = f.read()
        hashes = " ".join(
            "'sha256-" + base64.b64encode(hashlib.sha256(body.encode("utf-8")).digest()).decode()
            + "'" for body in inline_scripts(html))
        return ("default-src 'self'; script-src 'self' " + hashes + "; "
                "style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; "
                "frame-ancestors 'none'; object-src 'none'; base-uri 'none'; form-action 'self'")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                present = {k.lower() for k, _ in headers}
                headers += [(k, v) for k, v in self.COMMON if k not in present]
                if path in self.CSP_PATHS:
                    headers.append((b"content-security-policy", self.csp))
                message = {**message, "headers": headers}
            await send(message)
        await self.app(scope, receive, send_with_headers)


# wire the limiter: state, the 429 handler, and the middleware that enforces it
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)
app.add_middleware(_APIVersionPrefix)    # added last = runs first, before routing
app.add_middleware(_BodySizeLimit)       # before routing: no oversized body is parsed
app.add_middleware(_SecurityHeaders)     # outermost: every response, /v1 or not, 413s included


class _MCPEndpoint:
    """/mcp: hands the request to the MCP handler built in the lifespan, or 503s.

    A plain ASGI route rather than app.mount("/mcp"), so the URL is exactly /mcp
    (a mount would only answer /mcp/...). Auth is the handler's own (MCPAuth), so
    the MCP key can differ from the API key.
    """

    # slowapi's middleware names every route's endpoint; an ASGI-callable
    # instance has no __name__ of its own and would 500 every /mcp request.
    __name__ = "mcp_endpoint"

    async def __call__(self, scope, receive, send):
        handler = _STATE.get("mcp_http")
        if handler is None:
            body = json.dumps({"detail": "MCP over HTTP is not available",
                               "error": _STATE.get("mcp_error")}).encode()
            await send({"type": "http.response.start", "status": 503,
                        "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": body})
            return
        await handler(scope, receive, send)


from starlette.routing import Route  # noqa: E402

app.router.routes.append(Route("/mcp", endpoint=_MCPEndpoint(),
                               methods=["GET", "POST", "DELETE"], include_in_schema=False))


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
    question: str = Field(..., max_length=MAX_QUESTION_CHARS,
                          examples=["Which hospitals give the best value, high quality and low cost?"])
    backend: Optional[str] = Field(None, max_length=MAX_NAME_CHARS,
                                   description="data backend: local (default) | databricks")
    dataset: Optional[str] = Field(None, max_length=MAX_NAME_CHARS,
                                   description="dataset: hospital | fhir")


class SqlRequest(BaseModel):
    sql: str = Field(..., max_length=MAX_SQL_CHARS,
                     examples=["SELECT facility_name, star_rating FROM gold_hospital_profile WHERE star_rating = 5 LIMIT 10"])
    backend: Optional[str] = Field(None, max_length=MAX_NAME_CHARS,
                                   description="data backend: local (default) | databricks")
    dataset: Optional[str] = Field(None, max_length=MAX_NAME_CHARS,
                                   description="dataset: hospital | fhir")


class ProposeRequest(BaseModel):
    question: str = Field(..., max_length=MAX_QUESTION_CHARS,
                          examples=["Which hospitals have the worst heart failure readmission rates?"])
    capability: str = Field("brief.commit", max_length=MAX_NAME_CHARS, examples=["brief.commit"])


class DecisionRequest(BaseModel):
    """A reviewer's verdict on a pending proposal."""
    decision: str = Field(..., max_length=MAX_NAME_CHARS, examples=["approve"],
                          description="approve | reject | escalate | approve_with_edits")
    decided_by: str = Field(..., max_length=MAX_NAME_CHARS, examples=["e.brucaj"])
    reason: Optional[str] = Field(None, max_length=MAX_TEXT_CHARS)
    edited_proposal: Optional[str] = Field(
        None, max_length=MAX_TEXT_CHARS,
        description="required when decision is approve_with_edits")
    escalated_to: Optional[str] = Field(
        None, max_length=MAX_NAME_CHARS, description="required when decision is escalate")


class GovernedResponse(BaseModel):
    allowed: bool
    decided_by: str                      # "policy" | "guard" | "executed" | "critic" | "exhausted" | "sql_error" | "planner_error"
    reason: Optional[str] = None
    sql: Optional[str] = None            # the SQL that was evaluated
    safe_sql: Optional[str] = None       # what the guard actually ran
    row_count: Optional[int] = None
    truncated: Optional[bool] = None     # true when the result had more rows than the cap (item 43)
    columns: Optional[list] = None
    rows: Optional[list] = None
    planner_metrics: Optional[dict] = None   # per-call latency, tokens, est cost
    backend: Optional[str] = None            # which data backend served the query
    dataset: Optional[str] = None            # which dataset answered
    unanswerable: Optional[str] = None       # set when the dataset cannot answer
    suggest_dataset: Optional[str] = None    # a dataset that might answer instead
    trace_id: Optional[str] = None           # look the run up at /traces/{trace_id}
    # QUERY_MODE=graph only (PLAN v2 D2); absent in single mode
    attempts: Optional[int] = None           # plan->(critic)->execute cycles used
    critic: Optional[str] = None             # the last critic verdict
    trajectory: Optional[list] = None        # every step, in order (no rows)
    # backlog item 4: prompt_version, model, provider, git_commit of this answer
    version: Optional[dict] = None


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
        if "query execution failed" in reason:
            decided_by = "sql_error"     # it passed the guard and failed in the database
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
        **({"truncated": True} if out.get("truncated") else {}),
    }


def _record_failed_call(exc: BaseException, question: str) -> None:
    """Record the cost of a model call that completed but raised (a PlannerError
    for a refusal, truncation or thinking-only answer carries `.metrics`): the
    tokens were billed even though no SQL came back."""
    m = getattr(exc, "metrics", None)
    if m is not None:
        cost_tracker.record(m.as_dict(), question=question)


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
    tracer = tracing.get_tracer()
    with tracer.span("guard", sql_sha256=tracing.sql_sha256(sql), backend=backend,
                     dataset=dataset) as g:
        result = runtime.execute_tool(CAPABILITY, params)
        out = interpret_result(result, sql)
        g.set(allowed=out.get("allowed"), decided_by=out.get("decided_by"),
              reason=(out.get("reason") or "")[:500] or None)
        if out.get("decided_by") == "executed":
            tracer.event("execute", row_count=out.get("row_count"), backend=out.get("backend"),
                         dataset=out.get("dataset"))
    return out


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "version": APP_VERSION,                 # backlog item 5
        "api_version": API_VERSION,
        "git_commit": versioning.git_commit(),
        # where approvals and checkpoints live (item 19); never the credentials
        "state_store": __import__("state_db").describe(),
        "gold_db": os.path.basename(GOLD_DB),   # file name only, never a server path (item 40)
        "gold_db_present": os.path.exists(GOLD_DB),
        "runtime_ready": _STATE.get("runtime") is not None,
        "planner_enabled": _STATE.get("planner") is not None,
        "planner_error": _STATE.get("planner_error"),
        "auth_enabled": bool(API_KEY),
        "open_access_override": ALLOW_OPEN_ACCESS,
        "rate_limits": {"llm_endpoints": RATE_LIMIT_QUERY, "read_endpoints": RATE_LIMIT_READ},
        "query_mode": "graph" if _STATE.get("graph_agent") is not None else "single",
        "graph_error": _STATE.get("graph_error"),
        "mcp_http": _STATE.get("mcp_http") is not None,
        "mcp_error": _STATE.get("mcp_error"),
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


_DATASET_COUNTS: dict[tuple[str, float], list[dict]] = {}
_DATASET_COUNTS_LOCK = threading.Lock()


def _table_counts(active: str, db: str) -> list[dict]:
    """Row counts per table, through the guard, computed once per data vintage
    (the Gold file's mtime) and dataset (item 36): an unauthenticated caller
    can no longer make the server run one query per table on every request."""
    mtime = os.path.getmtime(db) if os.path.exists(db) else 0.0
    key = (active, mtime)
    with _DATASET_COUNTS_LOCK:
        hit = _DATASET_COUNTS.get(key)
    if hit is not None:
        return [dict(t) for t in hit]
    tables = []
    for t in sorted(schema_check.tables_in_schema_doc(DATASETS[active]["schema_doc"])):
        res = run_governed_sql(f"SELECT count(*) AS n FROM {t}", dataset=active)
        rows = res.get("rows") or []
        tables.append({"table": t, "rows": rows[0].get("n") if rows else None})
    if all(t["rows"] is not None for t in tables):   # a failed count is retried next time
        with _DATASET_COUNTS_LOCK:
            for k in [k for k in _DATASET_COUNTS if k[0] == active]:
                del _DATASET_COUNTS[k]
            _DATASET_COUNTS[key] = [dict(t) for t in tables]
    return tables


@app.get("/datasets")
@limiter.limit(RATE_LIMIT_READ)
def datasets(request: Request, dataset: Optional[str] = None) -> dict:
    """What the agent is pulling from: the active Gold dataset, its tables + row
    counts, when it was last built/refreshed, and monthly-refresh status. Powers
    the 'What's inside' view. Read-only, no auth (like /health): the UI's Data
    view and the nightly live check load it without a key.

    Table names come from the schema doc (the guard blocks information_schema);
    row counts run THROUGH the guard (run_governed_sql), so nothing bypasses it.
    Item 36: rate-limited (RATE_LIMIT_READ, per client address without a key)
    and the counts are cached per data vintage.
    """
    active = _dataset(dataset)
    db = DATASETS[active]["db"]
    info: dict[str, Any] = {
        "active_dataset": active,
        "available_datasets": [{"id": k, "label": v["label"],
                                "available": os.path.exists(v["db"])}
                               for k, v in DATASETS.items()],
        "source": _DATASET_SOURCES.get(active, {}),
        "gold_db": os.path.basename(db),
        "gold_db_present": os.path.exists(db),
        "last_built": None,
        "tables": [],
        "refresh": None,
        "backends": _STATE["analytics_tool"].backends() if _STATE.get("analytics_tool") else None,
    }
    if os.path.exists(db):
        ts = os.path.getmtime(db)
        info["last_built"] = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    info["tables"] = _table_counts(active, db)
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
    tracer = tracing.get_tracer()
    with tracer.trace("run", mode="raw-sql", dataset=_dataset(req.dataset),
                      sql=req.sql[:500]) as tid:
        out = run_governed_sql(req.sql, req.backend, _dataset(req.dataset))
        tracer.event("answer", outcome="answered" if out.get("allowed") else "refused",
                     decided_by=out.get("decided_by"))
        tracer.end_trace(tid, decided_by=out.get("decided_by"), row_count=out.get("row_count"))
    out["trace_id"] = tid
    return out


@app.post("/query", response_model=GovernedResponse, dependencies=[Depends(require_api_key)])
@limiter.limit(RATE_LIMIT_QUERY)
def query(request: Request, req: QueryRequest) -> dict:
    with _run_slot(), _model_budget(_STATE.get("planner")) as settle:
        out = _query(req)
        settle(_reported_cost(out))
        return out


def _query(req: QueryRequest) -> dict:
    planner = _STATE.get("planner")
    if planner is None:
        version = versioning.answer_version(None, DATASETS[_dataset(req.dataset)]["schema_doc"])
        _audit_answer(False, "planner_error", version, None)
        return {
            "allowed": False,
            "decided_by": "planner_error",
            "reason": f"planner not configured: {_STATE.get('planner_error')}",
            "version": version,
        }
    ds = _dataset(req.dataset)
    if _STATE.get("graph_agent") is not None:
        return _query_graph(req, ds)
    tracer = tracing.get_tracer()
    # QUERY_MODE=single (or no graph): one plan, one guard decision. Still a trace.
    with tracer.trace("run", mode="single", question=req.question, dataset=ds) as tid:
        try:
            with tracer.span("plan", attempt=1) as sp:
                sql, metrics = planner.generate_sql_with_metrics(
                    req.question, schema_doc=DATASETS[ds]["schema_doc"])
                sp.set(sql=sql, sql_sha256=tracing.sql_sha256(sql), model=metrics.model,
                       provider=metrics.provider, prompt_tokens=metrics.prompt_tokens,
                       completion_tokens=metrics.completion_tokens,
                       est_cost_usd=metrics.est_cost_usd, latency_ms=metrics.latency_ms,
                       cached=bool(getattr(metrics, "cached", False)))
        except Exception as e:
            _record_failed_call(e, req.question)
            tracer.event("answer", outcome="planner_error")
            version = versioning.answer_version(getattr(e, "metrics", None),
                                                DATASETS[ds]["schema_doc"], planner)
            tracer.end_trace(tid, status="error", decided_by="planner_error",
                             error=f"{type(e).__name__}: {e}"[:500], **version)
            _audit_answer(False, "planner_error", version, tid)
            return {"allowed": False, "decided_by": "planner_error",
                    "reason": f"planner error ({_public_error(e)}); details are in trace {tid}",
                    "trace_id": tid, "version": version}
        out = run_governed_sql(sql, req.backend, ds)
        if not out.get("allowed") and getattr(metrics, "cache_key", None):
            import llm_cache          # refused or failed: do not replay it (item 6)
            llm_cache.get_cache().evict(metrics.cache_key)
        note = _unanswerable(out)
        if note:
            out["unanswerable"] = note
            other = next((k for k in DATASETS
                          if k != ds and os.path.exists(DATASETS[k]["db"])), None)
            out["suggest_dataset"] = other
            out["rows"], out["columns"], out["row_count"] = [], [], 0
        out["planner_metrics"] = metrics.as_dict()
        cost_tracker.record(metrics.as_dict(), question=req.question)
        tracer.event("answer", outcome="unanswerable" if note else
                     ("answered" if out.get("allowed") else "refused"),
                     decided_by=out.get("decided_by"), row_count=out.get("row_count"))
        version = versioning.answer_version(metrics, DATASETS[ds]["schema_doc"], planner)
        out["version"] = version
        tracer.end_trace(tid, decided_by=out.get("decided_by"), row_count=out.get("row_count"),
                         est_cost_usd=metrics.est_cost_usd, total_tokens=metrics.total_tokens,
                         **version)
    out["trace_id"] = tid
    _audit_answer(bool(out.get("allowed")), out.get("decided_by"), version, tid)
    return out


def _audit_answer(allowed: bool, decided_by: Optional[str], version: dict,
                  trace_id: Optional[str]) -> None:
    """One `answer` audit line per /query (item 4). Best effort, like the tracer."""
    logger = getattr(_STATE.get("runtime"), "audit_logger", None)
    if logger is None or not hasattr(logger, "log_answer"):
        return
    try:
        logger.log_answer(capability="analytics.query_aggregate", allowed=allowed,
                          decided_by=decided_by, version=version, trace_id=trace_id)
    except Exception:
        pass


def _query_graph(req: QueryRequest, ds: str) -> dict:
    """/query through the planner graph (PLAN v2 D2). Same response shape as the
    single-shot path, plus `attempts`, `critic` and the run's `trajectory`."""
    agent = _STATE["graph_agent"]
    tid = tracing.new_id()
    try:
        state = agent.run(req.question, trace_id=tid, dataset=ds, backend=req.backend)
    except Exception as e:
        # The graph closed the trace as an error before re-raising. The calls it
        # made before failing were paid for: take them from the checkpoint.
        try:
            done = agent.graph.get_state({"configurable": {"thread_id": tid}}).values or {}
            for st in done.get("trajectory") or []:
                if st.get("step") in ("plan", "critic") and st.get("metrics"):
                    cost_tracker.record(st["metrics"], question=req.question)
        except Exception:
            pass
        _record_failed_call(e, req.question)   # the call that raised was billed too
        version = versioning.answer_version(getattr(e, "metrics", None),
                                            DATASETS[ds]["schema_doc"], agent.planner)
        _audit_answer(False, "planner_error", version, tid)
        return {"allowed": False, "decided_by": "planner_error",
                "reason": f"planner error ({_public_error(e)}); details are in trace {tid}",
                "trace_id": tid,
                "version": version}
    calls = [st["metrics"] for st in state.get("trajectory") or []
             if st.get("step") in ("plan", "critic") and st.get("metrics")]
    for m in calls:
        cost_tracker.record(m, question=req.question)
    allowed = bool(state.get("allowed"))
    out: dict[str, Any] = {
        "allowed": allowed,
        "decided_by": state.get("decided_by"),
        "reason": state.get("reason"),
        "sql": state.get("sql"),
        "dataset": ds,
        "attempts": state.get("attempts"),
        "critic": state.get("critic_verdict"),
        "trajectory": [{k: v for k, v in st.items() if k != "metrics"}
                       for st in state.get("trajectory") or []],
        "planner_metrics": {
            "calls": len(calls),
            "latency_ms": sum(m.get("latency_ms") or 0 for m in calls),
            "prompt_tokens": sum(m.get("prompt_tokens") or 0 for m in calls),
            "completion_tokens": sum(m.get("completion_tokens") or 0 for m in calls),
            "total_tokens": sum(m.get("total_tokens") or 0 for m in calls),
            "est_cost_usd": round(sum(m.get("est_cost_usd") or 0.0 for m in calls), 6),
            "model": calls[0].get("model") if calls else None,
            "provider": calls[0].get("provider") if calls else None,
        },
        "trace_id": tid,
        "version": state.get("version") or versioning.answer_version(
            calls[-1] if calls else None, DATASETS[ds]["schema_doc"], agent.planner),
    }
    _audit_answer(allowed, state.get("decided_by"), out["version"], tid)
    if allowed:
        out.update(safe_sql=state.get("safe_sql"), row_count=state.get("row_count"),
                   columns=state.get("columns"), rows=state.get("rows"),
                   backend=state.get("backend_used"))
        if state.get("truncated"):
            out["truncated"] = True
        note = _unanswerable(out)
        if note:
            out["unanswerable"] = note
            other = next((k for k in DATASETS
                          if k != ds and os.path.exists(DATASETS[k]["db"])), None)
            out["suggest_dataset"] = other
            out["rows"], out["columns"], out["row_count"] = [], [], 0
    return out


_FINDINGS_LOCK = threading.Lock()


def _vintage() -> tuple[Optional[str], tuple]:
    """(data vintage, cache key). The vintage is the CMS refresh date the build
    stamped in REFRESH.json; the key adds the Gold file's mtime so a rebuild
    without a new stamp still invalidates the cache."""
    refresh = os.path.join(os.path.dirname(GOLD_DB) or ".", "REFRESH.json")
    vintage, raw = None, ""
    try:
        with open(refresh, encoding="utf-8") as f:
            raw = f.read()
        vintage = json.loads(raw).get("last_refreshed")
    except Exception:
        pass
    try:
        mtime = os.path.getmtime(GOLD_DB)
    except OSError:
        mtime = None
    return vintage, (raw, mtime)


@app.get("/findings", dependencies=[Depends(require_api_key)])
@limiter.limit(RATE_LIMIT_READ)
def get_findings(request: Request) -> dict:
    """The Overview's plain-English findings (PLAN v2 item 6, D7).

    Fixed, reviewed SQL through the guarded path, filled into templates: no LLM,
    $0. Cached per data vintage; a set with any unavailable finding is not cached,
    so a transient failure is retried on the next load.
    """
    import findings
    vintage, key = _vintage()
    cached = _STATE.get("findings_cache")
    if cached and cached[0] == key:
        return cached[1]
    with _FINDINGS_LOCK:                      # simultaneous first loads compute once
        cached = _STATE.get("findings_cache")
        if cached and cached[0] == key:
            return cached[1]
        # Read the LOCAL Gold: the vintage and the cache key describe that file,
        # so the numbers must come from it even when DATA_BACKEND=databricks.
        with tracing.get_tracer().trace("findings", vintage=vintage):
            cards = findings.compute(lambda sql: run_governed_sql(sql, "local", "hospital"), vintage)
        out = {"dataset": "hospital", "backend": "local", "vintage": vintage, "findings": cards}
        if all(c["status"] == "ok" for c in cards):
            _STATE["findings_cache"] = (key, out)
        return out


@app.get("/metrics")
def metrics() -> dict:
    """Aggregate cost and latency across all planner calls (optimization view),
    and the answer cache's hit rate (item 6)."""
    import llm_cache
    out = cost_tracker.summarize()
    out["llm_cache"] = llm_cache.get_cache().stats()
    out["llm_budget"] = llm_budget.BUDGET.status()   # item 37: today's runs and est. spend
    return out


# ---------------------------------------------------------------------------
# Traces: every run (/query, /raw-sql, /propose and its decisions) as spans.
# Read-only, behind the API key. Spans hold SQL and counts, never rows.
# ---------------------------------------------------------------------------

@app.get("/traces", dependencies=[Depends(require_api_key)])
@limiter.limit(RATE_LIMIT_READ)
def list_traces(request: Request, limit: int = 50, offset: int = 0) -> dict:
    tracer = tracing.get_tracer()
    return {"traces": tracer.list_traces(limit=limit, offset=offset),
            "total": tracer.count(), "max_runs": tracer.max_runs,
            "note": "the trace store lives on the instance disk and resets on redeploy"}


@app.get("/traces/{trace_id}", dependencies=[Depends(require_api_key)])
@limiter.limit(RATE_LIMIT_READ)
def get_trace(request: Request, trace_id: str) -> dict:
    trace = tracing.get_tracer().get_trace(trace_id)
    if trace is None:
        raise HTTPException(status_code=404, detail=f"no trace {trace_id}")
    # An approval trace's id is its checkpoint thread: say where the graph stands.
    agent = _STATE.get("approval_agent")
    if trace.get("name") == "approval" and agent is not None:
        try:
            trace["checkpoint"] = agent.state(trace_id)
        except Exception as e:
            trace["checkpoint"] = {"error": _public_error(e)}
    return trace


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
    with _run_slot(), _model_budget(getattr(agent, "planner", None) or _STATE.get("planner")), \
            _APPROVAL_LOCK:
        return agent.start(req.question)   # its cost is not reported: it keeps the worst case


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
        with _APPROVAL_LOCK:
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
