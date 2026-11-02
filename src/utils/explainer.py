"""Explainable denial messages for policy/runtime outcomes."""

from __future__ import annotations

from typing import Any

from src.utils.redaction import redact_data, redact_text


def _find_capability(policy: dict[str, Any], capability: str) -> dict[str, Any] | None:
    for cap in policy.get("capabilities", []):
        if cap.get("name") == capability:
            return cap
    return None


def _build_path_snippet(policy: dict[str, Any], capability: str, requested_path: str) -> str:
    cap = _find_capability(policy, capability)
    existing_allow: list[str] = []
    if cap:
        existing_allow = list(((cap.get("constraints") or {}).get("paths") or {}).get("allow") or [])

    suggested = redact_text(requested_path).replace("\\", "/")
    if suggested and suggested not in existing_allow and "[REDACTED" not in suggested:
        existing_allow.append(suggested)

    allow_items = existing_allow or ["/workspace/<narrow-scope>/**"]
    allow_list = ", ".join(f"\"{item}\"" for item in allow_items[:4])

    return (
        "capabilities:\n"
        f"  - name: {capability}\n"
        "    allowed: true\n"
        "    constraints:\n"
        "      paths:\n"
        f"        allow: [{allow_list}]"
    )


def _build_endpoint_snippet(policy: dict[str, Any], capability: str, requested_url: str) -> str:
    cap = _find_capability(policy, capability)
    existing_allow: list[str] = []
    if cap:
        existing_allow = list(((cap.get("constraints") or {}).get("endpoints") or {}).get("allow") or [])

    suggested = redact_text(requested_url)
    if suggested and suggested not in existing_allow and "[REDACTED" not in suggested:
        existing_allow.append(suggested)

    allow_items = existing_allow or ["https://<trusted-host>/**"]
    allow_list = ", ".join(f"\"{item}\"" for item in allow_items[:4])

    return (
        "capabilities:\n"
        f"  - name: {capability}\n"
        "    allowed: true\n"
        "    constraints:\n"
        "      endpoints:\n"
        f"        allow: [{allow_list}]"
    )


def _build_capability_snippet(capability: str) -> str:
    if capability.startswith("filesystem."):
        return (
            "capabilities:\n"
            f"  - name: {capability}\n"
            "    allowed: true\n"
            "    constraints:\n"
            "      paths:\n"
            "        allow: [\"/workspace/<narrow-scope>/**\"]\n"
            "        deny: [\"/workspace/**/.env\", \"/workspace/**/*.key\"]"
        )
    if capability.startswith("http."):
        return (
            "capabilities:\n"
            f"  - name: {capability}\n"
            "    allowed: true\n"
            "    constraints:\n"
            "      endpoints:\n"
            "        allow: [\"https://<trusted-host>/**\"]\n"
            "        deny: [\"http://**\"]"
        )
    return (
        "capabilities:\n"
        f"  - name: {capability}\n"
        "    allowed: true"
    )


PHI_SAFE_QUERY = ("SELECT year(CAST(birth_date AS DATE)) AS birth_year, state, count(*) AS patients "
                  "FROM gold_patient GROUP BY 1, 2")
PHI_SAFE_SHAPE = ("ask for an aggregate instead of patient rows, for example patients counted "
                  f"by birth year and state: {PHI_SAFE_QUERY} "
                  "(groups of fewer than 11 patients are withheld).")


def get_explanation(decision: Any, capability: str, parameters: dict[str, Any], policy: dict[str, Any]) -> str:
    """
    Build a human-readable explanation for runtime decisions.

    Denials include:
    - concise reason
    - failing constraint context
    - minimal least-privilege policy snippet suggestion
    """
    safe_params = redact_data(parameters or {})
    reason = redact_text(getattr(decision, "reason", "Operation was denied by policy."))
    # Redact details too: they are echoed into the explanation, and a denial on a
    # sensitive path (e.g. /workspace/secrets/.env) would otherwise leak that path
    # back to the caller -- the exact value the denial was protecting.
    details = redact_data(getattr(decision, "details", None) or {})

    if getattr(decision, "allowed", False):
        if getattr(decision, "needs_approval", False):
            return (
                f"Operation allowed with approval: {capability}\n"
                f"Reason: {reason}\n"
                "Next step: request approval through the configured approval flow."
            )
        return f"Operation allowed: {capability}\nReason: {reason}"

    lines = [
        f"Operation denied: {capability}",
        f"Reason: {reason}",
    ]

    # Check the REASON first. An approval-required denial often still carries a
    # path/url parameter; without this ordering it would be misreported as a
    # path-constraint denial, sending the caller to fix the wrong thing.
    reason_lower = reason.lower()
    if "approval required" in reason_lower:
        lines.append("Constraint: operation requires human approval before execution.")
        lines.append("Next step: request approval and retry the action.")
        return "\n".join(lines)

    if "path" in details or "path" in safe_params:
        path_value = str(details.get("path") or safe_params.get("path") or "<path>")
        lines.append(f"Constraint: Requested path is outside allowed policy paths ({path_value}).")
        lines.append("Suggested policy snippet:")
        lines.append(_build_path_snippet(policy, capability, path_value))
        lines.append("Safe alternative: use a path already covered by existing allow rules.")
        return "\n".join(lines)

    if "url" in details or "url" in safe_params or "endpoint" in safe_params:
        endpoint = str(details.get("url") or safe_params.get("url") or safe_params.get("endpoint") or "<url>")
        lines.append(f"Constraint: Endpoint is not allowed by policy ({endpoint}).")
        lines.append("Suggested policy snippet:")
        lines.append(_build_endpoint_snippet(policy, capability, endpoint))
        lines.append("Safe alternative: use a currently allow-listed HTTPS endpoint.")
        return "\n".join(lines)

    if "explicitly denied" in reason_lower:
        lines.append("Constraint: this capability is configured with allowed: false.")
        lines.append("Suggested policy snippet:")
        lines.append(_build_capability_snippet(capability))
        lines.append("Safe alternative: grant only the narrow capability needed.")
        return "\n".join(lines)

    if "not in policy" in reason_lower or "default_policy" in reason_lower:
        lines.append("Constraint: capability is missing from policy and default deny blocked it.")
        lines.append("Suggested policy snippet:")
        lines.append(_build_capability_snippet(capability))
        lines.append("Safe alternative: add only this capability with narrow constraints.")
        return "\n".join(lines)

    if "phi rule" in reason_lower:
        # Patient-privacy refusal: the fix is a different question, never a
        # looser policy. (Not "Safe alternative:", which the MCP server strips
        # as operator advice -- a model caller should see this shape.)
        lines.append(f"Safe query shape: {PHI_SAFE_SHAPE}")
        return "\n".join(lines)

    lines.append("Safe alternative: adjust policy constraints minimally for this operation.")
    return "\n".join(lines)
