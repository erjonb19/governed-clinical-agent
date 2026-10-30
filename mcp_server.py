"""
Guarded MCP Server (stdio and Streamable HTTP)
==============================================
Exposes your Security-Constrained Agent Runtime as an MCP server.
Every MCP tool call is routed through your REAL policy engine
(AgentRuntime.execute_tool) before anything runs.

Place this file at the repo ROOT (same level as the `src/` folder)
and run it from there.

Run:
    pip install "mcp[cli]"
    python mcp_server.py                          # stdio (the default)
    python mcp_server.py --transport http         # Streamable HTTP on 127.0.0.1:8765/mcp
    python mcp_server.py --transport http --host 0.0.0.0 --port 8765

The FastAPI service also serves the same tools at /mcp (app.py, PLAN v2 D5).

HTTP AUTH (PLAN v2 item 5). Every HTTP request needs the key, as `X-API-Key: <key>`
or `Authorization: Bearer <key>`, compared in constant time. The key is
MCP_API_KEY, falling back to API_KEY (D6). Without either the HTTP transport
refuses to start, unless ALLOW_OPEN_ACCESS=true (local dev only) -- the same rule
as app.py. A request carrying a browser Origin must come from an allowed origin
(localhost, or MCP_ALLOWED_ORIGINS), the MCP spec's guard against DNS rebinding.
This is API-key auth. It is NOT OAuth 2.1, which stays on the roadmap.

Test (either one):
    npx @modelcontextprotocol/inspector python mcp_server.py
    # or add it to Claude Desktop's MCP config and call the tools

WHO WRITES THE SQL
The FastAPI service pairs an LLM planner with the guard: our planner drafts SQL,
the guard rules on it. Over MCP the client already HAS a model, so pairing a
second one inside the server would be redundant -- and would make the server
refuse to start without an LLM key, for a capability whose whole point is that
it is governed rather than clever.

So the split here is the idiomatic MCP one:

    analytics_schema()   the server tells the client's model what it may query
    query_analytics()    the client's model sends SQL; the GUARD rules on it

The client's model is the planner. This server is the part that cannot be
talked out of its rules. Nothing about the governance changes -- the same
policy engine and the same sql_guard.validate_query decide, and a denial is
still a denial no matter which model wrote the query.

WHAT THIS DEMONSTRATES
    analytics_schema()   -> ALLOWED  (describes only the Gold allowlist)
    query_analytics(...) -> GUARDED  (SELECT-only, Gold tables, row cap; a
                                      catalog probe or a bronze_* read is DENIED)
    echo("hi")           -> ALLOWED  (capability demo.echo is permitted)
    fetch_url(...)       -> DENIED   (http.fetch is high-risk, needs approval,
                                      no approver wired -> default deny)
    git_push(...)        -> DENIED   (git.push is allowed:false in the policy)

The policy engine, capability model, six-layer defense, and audit log are
all UNCHANGED. This file is just a new front door (MCP) onto execute_tool().
"""

from __future__ import annotations

import json
import os
import sys
from typing import Annotated, Any, Optional

# make `from src...` imports work when run from the repo root
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field

from src.runtime.agent_runtime import AgentRuntime
from src.runtime.bootstrap import register_default_tools
from src.tools.base import BaseTool, ToolResult, get_tool

from analytics_query_tool import AnalyticsQueryTool
from sql_guard import ALLOWED_TABLES, validate_query
from nl_to_sql_planner import SCHEMA_DOC_FHIR, SCHEMA_DOC_HOSPITAL

HERE = os.path.dirname(os.path.abspath(__file__))

# Same env vars the FastAPI service uses, so one deployment's Gold serves both
# front doors rather than each inventing its own paths.
DATASETS = {
    "hospital": {
        "db": os.environ.get("HOSPITAL_GOLD_DB", "medallion/hospital_gold.duckdb"),
        "schema_doc": SCHEMA_DOC_HOSPITAL,
        "label": "Hospital quality (CMS)",
    },
    "fhir": {
        "db": os.environ.get("FHIR_GOLD_DB", "medallion/fhir_gold.duckdb"),
        "schema_doc": SCHEMA_DOC_FHIR,
        "label": "Clinical records (FHIR)",
    },
}
DEFAULT_DATASET = os.environ.get("PLANNER_SCHEMA", "hospital").lower()
ANALYTICS_CAPABILITY = "analytics.query_aggregate"


# ---------------------------------------------------------------------------
# A tiny demo tool so Milestone 1 has a clean "allowed" path to show.
# ---------------------------------------------------------------------------
class EchoTool(BaseTool):
    @property
    def name(self) -> str:
        return "demo.echo"

    def execute(self, params):
        return ToolResult(success=True, output=f"echo: {params.get('text', '')}")


# ---------------------------------------------------------------------------
# Boot the REAL runtime once, at server startup.
# ---------------------------------------------------------------------------
POLICY_PATH = os.path.join(HERE, "mcp_demo_policy.yaml")

def _ensure_registered(rt: AgentRuntime, tool: BaseTool) -> None:
    """Register `tool` unless its capability already has one.

    The tool registry is process-global. When app.py serves these tools at /mcp,
    it has already registered analytics.query_aggregate for /query, with the same
    Gold paths; a second registration would raise. Reusing the existing one keeps
    a single implementation per capability, and the policy here still rules on it.
    """
    if get_tool(tool.name) is None:
        rt.register_tool(tool)


runtime = AgentRuntime()
runtime.load_policy(POLICY_PATH)
register_default_tools(runtime)      # git, http.fetch, package_manager.query
_ensure_registered(runtime, EchoTool())    # demo.echo


# The real product capability. Registered with the same multi-dataset wiring the
# API uses; seed_demo=False because the Gold is the point -- if it is missing we
# say so per call rather than quietly serving toy rows that look real.
#
# Unopenable databases are AnalyticsQueryTool's problem, not this file's: it
# skips a dataset it cannot connect to and records why, so a Gold held by
# another process (an eval sweep, a second server) costs us that one dataset
# instead of the whole server. Keeping that logic in one place means the API and
# MCP front doors degrade identically.
_analytics = AnalyticsQueryTool(
    db_path=DATASETS[DEFAULT_DATASET]["db"],
    seed_demo=False,
    db_paths={k: v["db"] for k, v in DATASETS.items()},
)
_ensure_registered(runtime, _analytics)


def available_datasets() -> list[str]:
    """Datasets this server can actually serve.

    Taken from the tool's own connections rather than from os.path.exists, so
    what we advertise is what we can answer.
    """
    return _analytics.datasets()


# ---------------------------------------------------------------------------
# The mediation seam: every MCP tool call goes through here.
# This is the heart of the project. MCP call -> capability -> execute_tool.
# ---------------------------------------------------------------------------
# A policy denial from the explainer ends with advice written for the OPERATOR
# tuning the policy file -- including, for a hard deny, a ready-to-paste YAML
# snippet that grants the very capability just refused:
#
#     Suggested policy snippet:
#     capabilities:
#       - name: git.push
#         allowed: true
#     Safe alternative: grant only the narrow capability needed.
#
# That is correct for a human editing policy and wrong for the caller here,
# because over MCP the caller IS a model. Handing it the patch that would grant
# the denied capability reframes the rule as an obstacle with a published
# workaround. Truncate at the first operator-advice marker and let the refusal
# stand on its own. `Suggested policy snippet:` always precedes
# `Safe alternative:` (src/utils/explainer.py), and the branches that emit
# `Next step:` emit no snippet at all -- so this keeps every line the caller can
# actually act on and drops only the ones aimed past it.
_OPERATOR_ADVICE_MARKERS = ("Suggested policy snippet:", "Safe alternative:")


def _clean_explanation(text: str) -> str:
    kept: list[str] = []
    for line in (text or "").splitlines():
        if line.strip().startswith(_OPERATOR_ADVICE_MARKERS):
            break
        kept.append(line)
    return "\n".join(kept).strip() or "not permitted"


def call_guarded(capability: str, params: dict) -> tuple[bool, Any, Optional[str]]:
    """Run a capability through the policy engine.

    Returns (ok, output, error). Separated from the string formatting below so
    the analytics path can render structured results while the demo tools keep
    their one-line answers.

    A guard refusal arrives here as allowed=False WITH a populated result: the
    runtime wraps the tool's own failure. That inner error is the useful one --
    it is sql_guard's single-line reason, naming the rule and the offending
    identifier -- so prefer it over the outer policy explanation.
    """
    result = runtime.execute_tool(capability, params)
    tool_result = getattr(result, "result", None)
    output = getattr(tool_result, "output", None)

    if not getattr(result, "allowed", False):
        inner = getattr(tool_result, "error", None)
        if inner:
            return False, output, f"DENIED [{capability}]: {inner}"
        reason = _clean_explanation(result.explanation)
        if reason == "not permitted" and result.decision is not None:
            reason = getattr(result.decision, "reason", "not permitted")
        return False, output, f"DENIED by policy [{capability}]: {reason}"

    if tool_result is not None and tool_result.success:
        return True, tool_result.output, None
    err = tool_result.error if tool_result is not None else "no result"
    return False, output, f"TOOL ERROR [{capability}]: {err}"


def guarded(capability: str, params: dict) -> str:
    ok, output, error = call_guarded(capability, params)
    return str(output) if ok else str(error)


def _refusal(error: str, findings, dataset: str) -> str:
    """A refusal is only useful if it says what to do instead.

    The guard names the rule and the offending identifier; append the allowlist
    so the caller can correct the query in one step rather than probing for it
    one table at a time -- which is the behaviour a terse denial produces, and
    which looks indistinguishable from an attack in the audit log.
    """
    msg = f"REFUSED: {error}"
    if findings:
        msg += f"\nOffending identifiers: {', '.join(str(f) for f in findings)}"
    msg += (f"\n\nHow to fix: query only these tables -- "
            f"{', '.join(sorted(ALLOWED_TABLES))}. "
            f"Call analytics_schema('{dataset}') for their columns. "
            f"Raw bronze_* tables are excluded deliberately: they hold "
            f"identifiable data and no query will ever reach them.")
    return msg


# ---------------------------------------------------------------------------
# The MCP server. Each @mcp.tool() is an MCP-exposed tool that any client
# (Claude Desktop, VS Code, the Inspector) can call. Each one maps to a
# capability and is mediated by your policy engine.
# ---------------------------------------------------------------------------
# Stateless JSON responses: every tool here is a single request/response, so no
# session state or SSE stream is needed, and any instance can answer any call.
# FastMCP's own Host-header check is off because it only admits localhost and
# would reject the deployed hostname; the Origin check in MCPAuth replaces it.
mcp = FastMCP(
    "guarded-runtime",
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


@mcp.tool()
def analytics_schema(
    dataset: Annotated[
        str,
        Field(description="Which dataset to describe: 'hospital' (CMS hospital "
                          "quality) or 'fhir' (de-identified clinical records).",
              pattern="^(hospital|fhir)$"),
    ] = DEFAULT_DATASET,
) -> str:
    """Describe the tables you may query, so you can write valid SQL.

    Call this BEFORE query_analytics. It returns the column-level schema of the
    curated Gold tables and the full allowlist. Anything not listed here will be
    refused by the guard -- including every raw `bronze_*` table, which is
    excluded on purpose because it carries identifiable patient data.
    """
    ds = dataset.lower()
    if ds not in DATASETS:
        return (f"Unknown dataset {dataset!r}. Valid values: "
                f"{', '.join(sorted(DATASETS))}.")
    present = available_datasets()
    if ds not in present:
        return (f"Dataset {ds!r} is not available on this deployment "
                f"(missing {DATASETS[ds]['db']}).\n"
                f"Available now: {', '.join(present) if present else 'none'}.")
    return (
        f"DATASET: {ds} -- {DATASETS[ds]['label']}\n\n"
        f"{DATASETS[ds]['schema_doc']}\n\n"
        f"RULES ENFORCED BY THE GUARD (not suggestions):\n"
        f"  - SELECT (or WITH ... SELECT) only. No INSERT/UPDATE/DELETE/DDL.\n"
        f"  - One statement per call.\n"
        f"  - Only these tables: {', '.join(sorted(ALLOWED_TABLES))}\n"
        f"  - No information_schema, no pg_catalog, no duckdb_* catalog functions.\n"
        f"  - No file-reading functions (read_csv, read_parquet, glob, ...).\n"
        f"  - A row cap is applied automatically; you do not need a LIMIT.\n"
    )


@mcp.tool()
def query_analytics(
    sql: Annotated[
        str,
        Field(description="A single read-only SQL SELECT (DuckDB dialect) over "
                          "the Gold tables listed by analytics_schema. No "
                          "trailing semicolon needed; one statement only.",
              min_length=1, max_length=20_000),
    ],
    dataset: Annotated[
        str,
        Field(description="Which Gold database to run against: 'hospital' or "
                          "'fhir'. Must match the schema you wrote the SQL for.",
              pattern="^(hospital|fhir)$"),
    ] = DEFAULT_DATASET,
) -> str:
    """Run a guarded read-only analytics query and return the rows as JSON.

    The SQL is parsed and validated BEFORE it can run: SELECT-only, Gold table
    allowlist, no catalog access, no file-reading functions, and a row cap. A
    refusal is final -- it is a policy outcome, not a transient error, so do not
    retry the same query. Rewrite it to obey the rule named in the refusal, or
    tell the user the data cannot answer their question.
    """
    ds = dataset.lower()
    if ds not in DATASETS:
        return (f"REFUSED: unknown dataset {dataset!r}. "
                f"Valid values: {', '.join(sorted(DATASETS))}.")

    # GUARD FIRST, before deciding whether we can even serve this dataset.
    # Whether a query is permitted must not depend on which databases happen to
    # be mounted: a bronze_* probe is refused on a deployment carrying no Gold
    # at all, and says so for the same reason it would anywhere else. Answering
    # "not available" to a query the guard would have refused reports a
    # deployment detail in place of a policy decision, and would leave the guard
    # untested on exactly the configurations that have no data to protect it.
    #
    # The tool re-validates internally; this does not replace that, and the
    # execution path below is unchanged.
    decision = validate_query(sql)
    if not decision.allowed:
        return _refusal(f"DENIED [{ANALYTICS_CAPABILITY}]: "
                        f"DENIED by sql_guard: {decision.reason}",
                        decision.findings, ds)

    present = available_datasets()
    if ds not in present:
        return (f"REFUSED: dataset {ds!r} is not available on this deployment.\n"
                f"Available now: {', '.join(present) if present else 'none'}.")

    ok, output, error = call_guarded(ANALYTICS_CAPABILITY, {"sql": sql, "dataset": ds})
    if ok:
        out = output or {}
        return json.dumps({
            "dataset": ds,
            "row_count": out.get("row_count"),
            "columns": out.get("columns"),
            "rows": out.get("rows"),
            "sql_executed": out.get("safe_sql"),
        }, indent=2, default=str)

    findings = (output or {}).get("findings") if isinstance(output, dict) else None
    return _refusal(error, findings, ds)


@mcp.tool()
def echo(
    text: Annotated[str, Field(description="Text to echo back.", max_length=4_000)],
) -> str:
    """Echo text back. Low-risk capability (demo.echo) — should be ALLOWED."""
    return guarded("demo.echo", {"text": text})


@mcp.tool()
def fetch_url(
    url: Annotated[str, Field(description="Absolute http(s) URL to fetch.")],
) -> str:
    """Fetch a URL. High-risk capability (http.fetch) — requires approval,
    so it is DENIED unless a human approver is wired in."""
    return guarded("http.fetch", {"url": url})


@mcp.tool()
def git_push(
    remote: Annotated[str, Field(description="Git remote name.")] = "origin",
    branch: Annotated[str, Field(description="Branch to push.")] = "main",
) -> str:
    """Attempt a git push. Capability git.push is allowed:false — DENIED."""
    return guarded("git.push", {"remote": remote, "branch": branch})


# ---------------------------------------------------------------------------
# Streamable HTTP: API-key auth and an Origin check in front of the MCP app.
# ---------------------------------------------------------------------------
LOCAL_ORIGINS = ("http://127.0.0.1", "http://localhost", "http://[::1]")


def mcp_key() -> Optional[str]:
    """MCP_API_KEY, else API_KEY (D6): rotate the MCP key on its own if you like."""
    return os.environ.get("MCP_API_KEY") or os.environ.get("API_KEY") or None


def allowed_origins() -> list[str]:
    extra = [o.strip().rstrip("/") for o in os.environ.get("MCP_ALLOWED_ORIGINS", "").split(",")
             if o.strip()]
    return extra


def origin_allowed(origin: Optional[str], extra: list[str]) -> bool:
    """No Origin (a non-browser client) is fine; a browser Origin must be local
    (any port) or listed in MCP_ALLOWED_ORIGINS."""
    if not origin:
        return True
    origin = origin.rstrip("/")
    for base in LOCAL_ORIGINS:
        if origin == base or origin.startswith(base + ":"):
            return True
    return origin in extra


def presented_key(headers: dict) -> Optional[str]:
    if headers.get("x-api-key"):
        return headers["x-api-key"]
    auth = headers.get("authorization", "")
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip() or None
    return None


class MCPAuth:
    """ASGI middleware: Origin check, then the API key. Anything else is refused
    before the MCP app sees the request. `key=None` means open access, which the
    callers only allow under ALLOW_OPEN_ACCESS."""

    def __init__(self, app, key: Optional[str], origins: Optional[list[str]] = None):
        self.app, self.key = app, key
        self.origins = allowed_origins() if origins is None else origins

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        import hmac
        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope.get("headers", [])}
        if not origin_allowed(headers.get("origin"), self.origins):
            return await _json(send, 403, {"detail": "origin not allowed"})
        if self.key is not None:
            given = presented_key(headers)
            if given is None or not hmac.compare_digest(given.encode(), self.key.encode()):
                return await _json(send, 401, {"detail": "missing or invalid API key"},
                                   [(b"www-authenticate", b"Bearer")])
        return await self.app(scope, receive, send)


async def _json(send, status: int, body: dict, extra_headers=None) -> None:
    data = json.dumps(body).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(data)).encode())] + (extra_headers or [])})
    await send({"type": "http.response.body", "body": data})


def resolve_http_key() -> Optional[str]:
    """The key the HTTP transport will require, or None for explicit open access.
    Refuses to start without one, mirroring app.py's REFUSING TO START."""
    key = mcp_key()
    if key:
        return key
    if os.environ.get("ALLOW_OPEN_ACCESS", "").lower() in {"1", "true", "yes"}:
        print("WARNING: MCP over HTTP is OPEN (ALLOW_OPEN_ACCESS). Local dev only.",
              file=sys.stderr)
        return None
    raise SystemExit(
        "\nREFUSING TO START: no MCP_API_KEY or API_KEY set for the HTTP transport.\n"
        "  Set MCP_API_KEY=<secret> (or API_KEY), or ALLOW_OPEN_ACCESS=true for local dev.\n")


def build_http_handler(key: Optional[str]):
    """A fresh (session manager, ASGI handler) pair for Streamable HTTP.

    Fresh each time because a session manager can only run once; the FastAPI
    app builds one per lifespan, and its tests restart the app. The handler is
    the MCP endpoint itself, behind MCPAuth.
    """
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from mcp.server.fastmcp.server import StreamableHTTPASGIApp
    manager = StreamableHTTPSessionManager(
        app=mcp._mcp_server,
        json_response=mcp.settings.json_response,
        stateless=mcp.settings.stateless_http,
        security_settings=mcp.settings.transport_security,
    )
    return manager, MCPAuth(StreamableHTTPASGIApp(manager), key)


def build_standalone_app(key: Optional[str]):
    """The standalone HTTP server: the MCP endpoint at /mcp, plus /health."""
    import contextlib
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    manager, handler = build_http_handler(key)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with manager.run():
            yield

    async def health(request):
        return JSONResponse({"status": "ok", "transport": "streamable-http",
                             "auth": "api-key" if key else "open"})

    return Starlette(routes=[Route("/mcp", endpoint=handler, methods=["GET", "POST", "DELETE"]),
                             Route("/health", endpoint=health)],
                     lifespan=lifespan)


def main(argv=None) -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Guarded MCP server")
    ap.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args(argv)
    if args.transport == "stdio":
        mcp.run()
        return
    import uvicorn
    app = build_standalone_app(resolve_http_key())
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
