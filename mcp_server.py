"""
Guarded MCP Server (stdio)
==========================
Exposes your Security-Constrained Agent Runtime as an MCP server.
Every MCP tool call is routed through your REAL policy engine
(AgentRuntime.execute_tool) before anything runs.

Place this file at the repo ROOT (same level as the `src/` folder)
and run it from there.

Run:
    pip install "mcp[cli]"
    python mcp_server.py            # starts the server on stdio

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

import duckdb
from mcp.server.fastmcp import FastMCP
from pydantic import Field

from src.runtime.agent_runtime import AgentRuntime
from src.runtime.bootstrap import register_default_tools
from src.tools.base import BaseTool, ToolResult

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

runtime = AgentRuntime()
runtime.load_policy(POLICY_PATH)
register_default_tools(runtime)      # git, http.fetch, package_manager.query
runtime.register_tool(EchoTool())    # demo.echo


def _openable(path: str) -> bool:
    """Can this Gold actually be opened right now?

    Existing on disk is not the same as being readable. DuckDB refuses a second
    connection to a file another process holds, so a running eval sweep or a
    local API server is enough to make an open raise. AnalyticsQueryTool skips a
    MISSING file but lets that IOException out, which would take the whole MCP
    server down at import over one temporarily busy dataset -- while the other
    one was perfectly serveable.

    So probe first and pass on only what opens. A dataset excluded here is
    reported as unavailable per call, exactly like a missing one.
    """
    if not os.path.exists(path):
        return False
    try:
        duckdb.connect(path, read_only=True).close()
        return True
    except Exception as e:                       # locked, corrupt, wrong version
        print(f"note: dataset at {path} is not openable ({e}); "
              f"serving without it", file=sys.stderr)
        return False


_USABLE = {k: v["db"] for k, v in DATASETS.items() if _openable(v["db"])}

# The real product capability. Registered with the same multi-dataset wiring the
# API uses; seed_demo=False because the Gold is the point -- if it is missing we
# say so per call rather than quietly serving toy rows that look real.
_analytics = AnalyticsQueryTool(
    db_path=_USABLE.get(DEFAULT_DATASET, ":memory:"),
    seed_demo=False,
    db_paths=_USABLE,
)
runtime.register_tool(_analytics)


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
mcp = FastMCP("guarded-runtime")


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


if __name__ == "__main__":
    # stdio transport for local dev. Milestone 3 switches to Streamable HTTP.
    mcp.run()
