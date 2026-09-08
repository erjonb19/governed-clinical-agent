"""The guarded MCP surface.

WHY THIS FILE EXISTS
The MCP server was a 115-line Milestone-1 demo exposing three toy capabilities
(echo, fetch_url, git_push). The headline claim is a *governed* MCP server, and
the governed thing -- analytics.query_aggregate, the whole product -- was not on
it. There was also no test of any kind over the MCP front door.

These tests pin two things:

  1. the analytics capability is genuinely reachable over MCP, and every guard
     rule still applies to it (a front door must not be a side door);
  2. refusals are useful to the caller, who over MCP is a MODEL. That means the
     rule and the remedy, and specifically NOT the policy explainer's operator
     advice -- which, for a hard deny, includes a paste-ready YAML snippet
     granting the very capability just refused.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

pytest.importorskip("mcp", reason="the MCP front door needs the mcp package")

import mcp_server as M
from sql_guard import ALLOWED_TABLES


needs_gold = pytest.mark.skipif(
    not M.available_datasets(),
    reason="no Gold database on disk; build medallion/*.duckdb to run this")


@pytest.fixture(autouse=True)
def mcp_tools_registered():
    """Re-register what mcp_server registered at import time.

    The tool registry is process-global, and several suites here clear it in
    their own fixtures (tests/integration/test_execute_tool.py explains why).
    mcp_server registers its tools once, at import -- so by the time this file
    runs, an earlier suite may have wiped them and every capability would deny
    with "No tool registered", which looks exactly like a governance result and
    is really just test-order damage.

    Restore the registry afterwards so this file is no worse a neighbour than
    the ones it is working around.
    """
    import src.tools.base as tool_base

    saved = dict(tool_base._TOOL_REGISTRY)
    for tool in (M.EchoTool(), M._analytics):
        tool_base._TOOL_REGISTRY.pop(tool.name, None)
        M.runtime.register_tool(tool)
    try:
        yield
    finally:
        tool_base._TOOL_REGISTRY.clear()
        tool_base._TOOL_REGISTRY.update(saved)


# --------------------------------------------------------------------------
# The capability is actually exposed
# --------------------------------------------------------------------------

@pytest.mark.anyio
async def test_analytics_tools_are_exposed_over_mcp(anyio_backend):
    names = {t.name for t in await M.mcp.list_tools()}
    assert {"query_analytics", "analytics_schema"} <= names


@pytest.mark.anyio
async def test_query_analytics_has_a_strict_input_schema(anyio_backend):
    """A bare `sql: str` tells a calling model nothing. The schema has to carry
    the description and the constraints, because that is all the client sees
    before it writes a query."""
    tool = next(t for t in await M.mcp.list_tools() if t.name == "query_analytics")
    props = tool.inputSchema["properties"]

    assert tool.inputSchema["required"] == ["sql"]
    assert props["sql"]["description"]
    assert props["sql"]["minLength"] == 1
    assert props["sql"]["maxLength"] > 0
    # dataset is constrained to the real datasets, not free text
    assert props["dataset"]["pattern"] == "^(hospital|fhir)$"
    assert props["dataset"]["description"]


# --------------------------------------------------------------------------
# The guard still governs it
# --------------------------------------------------------------------------

@pytest.mark.parametrize("sql,rule", [
    ("SELECT * FROM bronze_patient", "allowlist"),
    ("SELECT * FROM information_schema.tables", "catalog"),
    ("DELETE FROM gold_patient", "only SELECT"),
    ("SELECT * FROM read_csv_auto('/etc/passwd')", "disallowed function"),
    ("DROP TABLE gold_patient", "only SELECT"),
])
def test_guard_rules_apply_over_mcp(sql, rule):
    """Every class the adversarial harness covers, refused through the MCP door.
    A new front door that skipped the guard would be the worst possible
    regression in this repo."""
    out = M.query_analytics(sql, "fhir")
    assert out.startswith("REFUSED"), out
    assert rule in out, out


def test_phi_tables_are_refused_by_name():
    """bronze_* is excluded on purpose: it carries identifiable patient data."""
    out = M.query_analytics("SELECT name, address FROM bronze_patient", "fhir")
    assert "bronze_patient" in out
    assert "not on Gold allowlist" in out


# --------------------------------------------------------------------------
# Refusals have to be usable by a model
# --------------------------------------------------------------------------

def test_refusal_names_the_rule_and_the_remedy():
    out = M.query_analytics("SELECT * FROM bronze_patient", "fhir")
    assert "How to fix" in out
    # the allowlist is spelled out, so the caller can correct in one step
    for table in ("gold_patient", "gold_encounter"):
        assert table in out


def test_refusal_does_not_hand_the_caller_a_policy_patch():
    """The explainer's hard-deny branch emits:

        Suggested policy snippet:
        capabilities:
          - name: git.push
            allowed: true

    Correct for a human editing policy; wrong for a model, which reads it as a
    published workaround for the rule it just hit."""
    out = M.git_push()
    assert "DENIED" in out
    assert "Suggested policy snippet" not in out
    assert "allowed: true" not in out
    assert "Safe alternative" not in out


def test_actionable_lines_survive_the_trim():
    """Over-trimming would be its own bug -- the approval branch's `Next step`
    is exactly the line a caller can act on."""
    out = M.fetch_url("https://example.com")
    assert "DENIED" in out
    assert "Next step" in out


def test_unknown_dataset_is_refused_not_silently_defaulted():
    out = M.query_analytics("SELECT 1", "payroll")
    assert out.startswith("REFUSED")
    assert "payroll" in out


# --------------------------------------------------------------------------
# The schema tool
# --------------------------------------------------------------------------

@needs_gold
def test_schema_lists_only_allowlisted_tables():
    ds = M.available_datasets()[0]
    doc = M.analytics_schema(ds)
    assert "bronze_" not in doc, "the schema must not advertise the PHI layer"
    assert "SELECT" in doc
    for table in sorted(ALLOWED_TABLES):
        if table in doc:
            break
    else:
        pytest.fail("schema names none of the allowlisted tables")


def test_schema_rejects_an_unknown_dataset():
    assert "Unknown dataset" in M.analytics_schema("payroll")


# --------------------------------------------------------------------------
# The allowed path
# --------------------------------------------------------------------------

@needs_gold
def test_an_allowed_query_returns_rows_as_json():
    ds = M.available_datasets()[0]
    table = "gold_hospital_profile" if ds == "hospital" else "gold_patient"
    out = M.query_analytics(f"SELECT count(*) AS n FROM {table}", ds)
    assert not out.startswith("REFUSED"), out

    payload = json.loads(out)
    assert payload["dataset"] == ds
    assert payload["row_count"] == 1
    assert payload["rows"][0]["n"] > 0
    # the guard's rewritten SQL is reported, not the caller's original
    assert "LIMIT" in payload["sql_executed"].upper()


@needs_gold
def test_row_cap_is_applied_without_being_asked_for():
    ds = M.available_datasets()[0]
    table = "gold_hospital_profile" if ds == "hospital" else "gold_patient"
    payload = json.loads(M.query_analytics(f"SELECT * FROM {table}", ds))
    assert payload["row_count"] <= 1000
    assert "LIMIT" in payload["sql_executed"].upper()


def test_echo_still_works():
    """The Milestone-1 allow path is unchanged."""
    assert M.echo("hi") == "echo: hi"


def test_the_guard_rules_before_dataset_availability(monkeypatch):
    """A refusal must not depend on which databases happen to be mounted.

    query_analytics consults the guard BEFORE reporting a dataset unavailable.
    If the order were reversed, a bronze_* probe on a deployment without that
    Gold would come back "not available" -- a deployment detail standing in for
    a policy decision -- and the guard would go untested on exactly the
    configurations that have no data to protect it. CI is one of those: it runs
    with no Gold at all.

    Mounting is forced here rather than inferred from the machine, so this holds
    the ordering on a developer box with both datasets built and in CI with
    neither.
    """
    monkeypatch.setattr(M, "available_datasets", lambda: [])

    out = M.query_analytics("SELECT * FROM bronze_patient", "fhir")
    assert "not on Gold allowlist" in out, out
    assert "not available" not in out, out


def test_an_allowed_query_against_an_absent_dataset_says_so(monkeypatch):
    """The other side of that ordering: once the SQL is permitted, an absent
    dataset is the honest answer and must not be dressed up as a denial."""
    monkeypatch.setattr(M, "available_datasets", lambda: [])

    out = M.query_analytics("SELECT count(*) AS n FROM gold_patient", "fhir")
    assert "not available" in out
    assert "DENIED" not in out
