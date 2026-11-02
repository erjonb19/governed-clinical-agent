"""Unit tests for src.utils.explainer."""

from src.runtime.policy_engine import Decision
from src.utils.explainer import get_explanation


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

    def test_default_deny_includes_capability_snippet(self) -> None:
        decision = Decision(allowed=False, reason="Capability 'x' not in policy; default_policy is deny.")
        msg = get_explanation(decision, "package_manager.query", {}, _policy())
        assert "Operation denied: package_manager.query" in msg
        assert "Suggested policy snippet:" in msg
        assert "name: package_manager.query" in msg

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

    def test_path_deny_includes_path_snippet(self) -> None:
        decision = Decision(
            allowed=False,
            reason="Path denied",
            details={"path": "/workspace/secrets/.env"},
            policy_rule="filesystem.read",
        )
        msg = get_explanation(decision, "filesystem.read", {"path": "/workspace/secrets/.env"}, _policy())
        assert "Constraint: Requested path is outside allowed policy paths" in msg
        assert "paths:" in msg
        assert "[REDACTED_PATH]" in msg

    def test_endpoint_deny_includes_endpoint_snippet(self) -> None:
        decision = Decision(
            allowed=False,
            reason="Endpoint denied",
            details={"url": "http://evil.com"},
            policy_rule="http.fetch",
        )
        msg = get_explanation(decision, "http.fetch", {"url": "http://evil.com"}, _policy())
        assert "Constraint: Endpoint is not allowed by policy" in msg
        assert "endpoints:" in msg

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
