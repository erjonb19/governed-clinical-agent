"""Unit tests for src.utils.explainer."""

from src.runtime.policy_engine import Decision
from src.utils.explainer import get_explanation, policy_snippet


def _policy() -> dict:
    return {
        "version": "1.0",
        "default_policy": "deny",
        "capabilities": [
            {
                "name": "filesystem.read",
                "allowed": True,
                "constraints": {"paths": {"allow": ["/workspace/src/**"], "deny": ["/workspace/**/.env"]}},
            },
            {
                "name": "http.fetch",
                "allowed": True,
                "constraints": {"endpoints": {"allow": ["https://api.github.com/**"], "deny": ["http://**"]}},
            },
            {"name": "shell.execute", "allowed": False},
        ],
    }


class TestExplainer:
    """Tests for denial explanation generation."""

    # Policy snippets (YAML that would grant what was refused) go to the audit
    # log only, never into what the caller sees (owner, 2026-11-02).

    def test_default_deny_snippet_is_for_the_audit_log_only(self) -> None:
        decision = Decision(allowed=False, reason="Capability 'x' not in policy; default_policy is deny.")
        msg = get_explanation(decision, "package_manager.query", {}, _policy())
        assert "Operation denied: package_manager.query" in msg
        assert "Suggested policy snippet" not in msg and "allowed: true" not in msg
        assert "name: package_manager.query" in policy_snippet(decision, "package_manager.query",
                                                              {}, _policy())

    def test_explicit_deny_includes_safe_guidance(self) -> None:
        decision = Decision(allowed=False, reason="Capability 'shell.execute' is explicitly denied by policy.")
        msg = get_explanation(decision, "shell.execute", {}, _policy())
        assert "explicitly denied" in msg.lower()
        assert "Safe alternative" in msg

    def test_phi_refusal_suggests_a_query_shape_not_a_policy_change(self) -> None:
        decision = Decision(allowed=False, reason="Tool execution failed: PHI rule: returns "
                            "identifier gold_patient.patient_id as column 'patient_id'.")
        msg = get_explanation(decision, "analytics.query_aggregate", {}, _policy())
        assert "Safe query shape:" in msg and "birth year and state" in msg
        assert "adjust policy" not in msg and "Suggested policy snippet" not in msg
        assert "Safe alternative" not in msg

    def test_path_deny_snippet_is_for_the_audit_log_only(self) -> None:
        decision = Decision(
            allowed=False,
            reason="Path denied",
            details={"path": "/workspace/secrets/.env"},
            policy_rule="filesystem.read",
        )
        params = {"path": "/workspace/secrets/.env"}
        msg = get_explanation(decision, "filesystem.read", params, _policy())
        assert "Constraint: Requested path is outside allowed policy paths" in msg
        assert "[REDACTED_PATH]" in msg
        assert "paths:" not in msg and "Suggested policy snippet" not in msg
        snippet = policy_snippet(decision, "filesystem.read", params, _policy())
        assert "paths:" in snippet and ".env" not in snippet.replace("/workspace/**/.env", "")

    def test_endpoint_deny_snippet_is_for_the_audit_log_only(self) -> None:
        decision = Decision(
            allowed=False,
            reason="Endpoint denied",
            details={"url": "http://evil.com"},
            policy_rule="http.fetch",
        )
        msg = get_explanation(decision, "http.fetch", {"url": "http://evil.com"}, _policy())
        assert "Constraint: Endpoint is not allowed by policy" in msg
        assert "endpoints:" not in msg and "Suggested policy snippet" not in msg
        assert "endpoints:" in policy_snippet(decision, "http.fetch", {"url": "http://evil.com"},
                                              _policy())

    def test_approval_denial_message(self) -> None:
        decision = Decision(allowed=False, reason="Approval required but not granted.")
        msg = get_explanation(decision, "filesystem.write", {"path": "/workspace/src/a.py"}, _policy())
        assert "requires human approval" in msg.lower()
        assert "request approval" in msg.lower()

    def test_sensitive_params_are_redacted_in_message(self) -> None:
        decision = Decision(allowed=False, reason="Denied")
        msg = get_explanation(
            decision,
            "shell.execute",
            {"authorization": "Bearer SECRET-TOKEN-VALUE"},
            _policy(),
        )
        assert "SECRET-TOKEN-VALUE" not in msg


# --- every refusal suggests a different request, never a looser policy ------
# (owner, 2026-11-02: "Safe alternative: adjust policy constraints minimally"
# used to end every guard refusal)

import pytest  # noqa: E402

LOOSENING = ("adjust policy", "loosen", "relax", "grant ", "add only this capability",
             "allowed: true", "widen")

GUARD_REASONS = {
    "no such column": ("DENIED by sql_guard: no such column: gold_condition.is_experiment; use "
                       "only the columns the schema lists", "columns the schema lists"),
    "allowlist": ("DENIED by sql_guard: table not on Gold allowlist: bronze_patient",
                  "tables the schema lists"),
    "catalog": ("DENIED by sql_guard: catalog/system schema not allowed: information_schema.tables",
                "tables the schema lists"),
    "write": ("DENIED by sql_guard: write or DDL operation denied: DROP", "read-only SELECT"),
    "not select": ("DENIED by sql_guard: only SELECT is allowed, got INSERT", "read-only SELECT"),
    "stacked": ("DENIED by sql_guard: stacked statements not allowed (2 found)", "read-only SELECT"),
    "function": ("DENIED by sql_guard: disallowed function: read_csv", "plain SQL"),
    "table function": ("DENIED by sql_guard: table function not allowed: query", "plain SQL"),
    "unparseable": ("DENIED by sql_guard: unparseable SQL, denied (fail closed): x",
                    "complete SELECT"),
    "execution": ("query execution failed: Binder Error: column x not found", "error named above"),
    "phi": ("DENIED by sql_guard: PHI rule: returns identifier gold_patient.patient_id",
            "birth year and state"),
    "output check": ("DENIED by sql_guard: output check: column 'x' holds dates with month or day "
                     "precision", "birth year and state"),
}


@pytest.mark.parametrize("kind", sorted(GUARD_REASONS))
def test_every_guard_refusal_suggests_a_query_not_a_policy_change(kind):
    reason, expected = GUARD_REASONS[kind]
    msg = get_explanation(Decision(allowed=False, reason=f"Tool execution failed: {reason}"),
                          "analytics.query_aggregate", {"sql": "SELECT 1"}, _policy())
    last = msg.splitlines()[-1]
    assert last.startswith("Safe query shape:") and expected in last, msg
    assert "Suggested policy snippet" not in msg
    for word in LOOSENING:
        assert word not in msg.lower(), (kind, word)


def test_no_such_column_points_to_the_schema_column_list():
    reason, _ = GUARD_REASONS["no such column"]
    msg = get_explanation(Decision(allowed=False, reason=reason), "analytics.query_aggregate",
                          {}, _policy())
    assert "GET /schema" in msg and "analytics_schema" in msg


@pytest.mark.parametrize("reason", [
    "Capability 'shell.execute' is explicitly denied by policy.",
    "Capability 'x' not in policy; default_policy is deny.",
    "something the explainer has never seen",
])
def test_policy_refusals_never_suggest_loosening_either(reason):
    msg = get_explanation(Decision(allowed=False, reason=reason), "shell.execute", {}, _policy())
    advice = [ln for ln in msg.splitlines() if ln.startswith("Safe alternative:")]
    assert len(advice) == 1, msg
    for word in LOOSENING:
        assert word not in advice[0].lower(), (reason, word)
    assert "owner's decision" in advice[0]
