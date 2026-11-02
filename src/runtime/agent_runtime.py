"""Main runtime orchestrator for agent security."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from src.tools.base import BaseTool, ToolError, ToolResult, get_tool, register_tool as base_register_tool
from src.utils.explainer import get_explanation as build_explanation
from src.utils.explainer import policy_snippet as build_policy_snippet
from src.utils.redaction import redact_data, redact_text

from .audit_logger import AuditLogger, DecisionType
from .capability import resolve_capability
from .capability import is_high_risk
from .policy_engine import Decision, PolicyEngine
from .audit_logger import AuditLogger, DecisionType
from src.tools.base import BaseTool, ToolResult, ToolError, get_tool, register_tool as base_register_tool
# [TASK 2.3] Import parameter validator to enable pre-execution validation of tool parameters.
# This replaces the TODO stub in parameter_validator.py (completed in task 2.1).
from src.security.parameter_validator import validate as validate_parameters
from src.security.injection_detector import InjectionDetector
from src.security.taint_tracking import TaintTracker
from src.runtime.sandbox import SandboxConfig, docker_available, run_tool_in_docker

logger = logging.getLogger(__name__)

_DEFAULT_INJECTION_DETECTOR = object()


@dataclass
class ExecuteResult:
    """Result of execute_tool: allowed, result (if allowed), explanation."""

    allowed: bool
    result: Optional[ToolResult] = None
    explanation: str = ""
    decision: Optional[Decision] = None


class AgentRuntime:
    """
    Orchestrator: evaluate policy, optional approval, execute tool, return result.

    - execute_tool(capability, parameters): main entry.
    - register_tool(tool): delegate to tools.base.register_tool.
    - request_approval(capability, parameters): sync callback or default deny.
    """

    def __init__(
        self,
        policy_engine: Optional[PolicyEngine] = None,
        approval_callback: Optional[Callable[[str, Dict[str, Any]], bool]] = None,
        audit_logger: Optional[AuditLogger] = None,
        injection_detector: Any = _DEFAULT_INJECTION_DETECTOR,
        taint_tracker: Any = _DEFAULT_INJECTION_DETECTOR,
    ) -> None:
        self._policy_engine = policy_engine or PolicyEngine()
        self._approval_callback = approval_callback
        self.audit_logger = audit_logger
        if injection_detector is _DEFAULT_INJECTION_DETECTOR:
            self._injection_detector: Optional[InjectionDetector] = InjectionDetector()
        else:
            self._injection_detector = injection_detector
        # Phase 3: taint tracker. Pass None to disable; pass a custom instance to
        # override the source/sink lists. Default is an enabled tracker with the
        # standard source/sink configuration from src.security.taint_tracking.
        if taint_tracker is _DEFAULT_INJECTION_DETECTOR:
            self._taint_tracker: Optional[TaintTracker] = TaintTracker()
        else:
            self._taint_tracker = taint_tracker

    def load_policy(self, path: Optional[str | Path] = None) -> None:
        """Load policy from file (YAML/JSON). Raises on validation error."""
        self._policy_engine.load_policy(path)

    def evaluate_policy(self, capability: str, parameters: Optional[Dict[str, Any]] = None) -> Decision:
        """Evaluate policy only (no tool lookup or execution). Use for pre-checks."""
        parameters = parameters or {}
        capability = resolve_capability(capability)
        return self._policy_engine.evaluate(capability, parameters)

    def get_explanation(
        self,
        decision: Decision,
        capability: Optional[str] = None,
        parameters: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Return human-readable explanation for a decision."""
        if capability is None:
            return self._policy_engine.get_explanation(decision)
        return build_explanation(decision, capability, parameters or {}, self._policy_engine.get_policy())

    def register_tool(self, tool: BaseTool) -> None:
        """Register a tool implementation (capability name = tool.name)."""
        base_register_tool(tool)

    def request_approval(self, capability: str, parameters: Dict[str, Any]) -> bool:
        """
        Return True if approved, False if rejected or no callback.
        When needs_approval and no callback is set, defaults to False (deny).
        """
        if self._approval_callback is None:
            return False
        return self._approval_callback(capability, parameters)

    @staticmethod
    def _apply_policy_resource_limits(
        parameters: Dict[str, Any], decision: Decision
    ) -> Dict[str, Any]:
        """Forward policy-surfaced resource limits to the tool (caller wins).

        Currently propagated:
        - ``max_response_size`` (used by ``http.fetch`` to cap streamed bytes).

        Caller-provided values are never overwritten; this only fills gaps so a
        tool default does not silently relax a stricter policy limit.
        """
        details = decision.details or {}
        if not details:
            return parameters
        merged = dict(parameters)
        if "max_response_size" in details and "max_response_size" not in merged:
            merged["max_response_size"] = details["max_response_size"]
        return merged

    def execute_tool(
        self,
        capability: str,
        parameters: Optional[Dict[str, Any]] = None,
    ) -> ExecuteResult:
        """
        Main entry:
        1. Evaluate policy. If deny, return denial + explanation.
        2. If allow and needs_approval, request approval; if rejected, deny.
        3. [TASK 2.3] Parameter validation → if invalid → deny and log.
        4. If allow, execute tool with redacted logging/outputs.
        """
        parameters = parameters or {}
        capability = resolve_capability(capability)
        redacted_parameters = redact_data(parameters)

        eval_start_time = datetime.now()
        decision = self._policy_engine.evaluate(capability, parameters)
        evaluation_time_ms = (datetime.now() - eval_start_time).total_seconds() * 1000

        if self.audit_logger:
            decision_type = DecisionType.ALLOW if decision.allowed else DecisionType.DENY
            if decision.needs_approval:
                decision_type = DecisionType.REQUIRE_APPROVAL
            # The least-privilege snippet for a denial is operator advice: it
            # is written to the audit log only, never into the explanation the
            # caller receives (owner's decision 2026-11-02).
            snippet = None if decision.allowed else build_policy_snippet(
                decision, capability, parameters, self._policy_engine.get_policy())
            self.audit_logger.log_policy_evaluation(
                capability=capability,
                decision=decision_type,
                reason=decision.reason or "Policy evaluation",
                parameters=redacted_parameters,
                policy_rule=decision.policy_rule,
                evaluation_time_ms=evaluation_time_ms,
                context={"policy_snippet": snippet} if snippet else None,
            )

        if not decision.allowed:
            explanation = self.get_explanation(decision, capability, parameters)
            logger.info("execute_tool denied: %s - %s", capability, explanation)
            return ExecuteResult(allowed=False, explanation=explanation, decision=decision)

        if decision.needs_approval:
            if self.audit_logger:
                self.audit_logger.log_approval_requested(
                    capability=capability,
                    parameters=redacted_parameters,
                    reason="Policy requires approval for this operation",
                )

            approved = self.request_approval(capability, parameters)

            if self.audit_logger:
                self.audit_logger.log_approval_decision(
                    request_event_id="",
                    capability=capability,
                    approved=approved,
                    approver="approval_callback",
                    comment="Callback decision",
                )

            if not approved:
                denial_decision = Decision(
                    allowed=False,
                    reason="Approval required but not granted.",
                    policy_rule=decision.policy_rule,
                )
                explanation = self.get_explanation(denial_decision, capability, parameters)
                logger.info("execute_tool approval denied: %s", capability)
                return ExecuteResult(allowed=False, explanation=explanation, decision=decision)

        # [TASK 2.3] Parameter validation — runs after policy allows and after approval check,
        # before tool lookup and execution. This enforces type, path traversal, enum, range,
        # and shell-pattern constraints defined in the policy. On failure, deny and log so
        # the audit trail captures exactly which constraint was violated.
        # Old code went straight to: tool = get_tool(capability)
        cap_config = self._policy_engine._find_capability(capability)
        constraints = cap_config.get("constraints") or {} if cap_config else {}

        validation = validate_parameters(capability, parameters, constraints)
        if not validation.valid:
            explanation = "Parameter validation failed: " + "; ".join(validation.errors)
            logger.info("execute_tool validation denied: %s - %s", capability, explanation)

            if self.audit_logger:
                self.audit_logger.log_parameter_validation(
                    capability=capability,
                    parameters=parameters,
                    validation_errors=validation.errors,
                    constraint_violated=validation.constraint_violated,
                )

            return ExecuteResult(
                allowed=False,
                explanation=explanation,
                decision=decision,
            )
        # [END TASK 2.3]

        if self._injection_detector is not None:
            inj = self._injection_detector.scan(capability, parameters)
            if not inj.clean:
                explanation = (
                    f"Blocked: possible injection in tool parameters ({inj.injection_type}). {inj.reason}"
                )
                logger.info(
                    "execute_tool injection blocked: %s at %s",
                    capability,
                    inj.parameter_path,
                )
                if self.audit_logger:
                    self.audit_logger.log_injection_detected(
                        capability=capability,
                        parameters=parameters,
                        injection_type=inj.injection_type,
                        pattern_matched=inj.pattern_matched,
                        context={
                            "parameter_path": inj.parameter_path,
                            "reason": inj.reason,
                        },
                    )
                return ExecuteResult(allowed=False, explanation=explanation, decision=None)

        # [Phase 3] Taint sink check: deny if any parameter contains data from
        # a previous tool's output and the destination capability is a sink.
        # This catches confused-deputy flows that pass injection scanning because
        # the substring is innocent on its own (e.g. a URL read from a file).
        if self._taint_tracker is not None and self._taint_tracker.is_sink(capability):
            taint = self._taint_tracker.check_sink(capability, parameters)
            if taint.violated:
                explanation = (
                    f"Blocked: tainted data flow into sink {capability!r}. {taint.reason}"
                )
                logger.info(
                    "execute_tool taint blocked: %s <- %s at %s",
                    capability,
                    taint.source_capability,
                    taint.parameter_path,
                )
                if self.audit_logger:
                    self.audit_logger.log_taint_violation(
                        capability=capability,
                        parameters=parameters,
                        source_capability=taint.source_capability,
                        source_id=taint.source_id,
                        parameter_path=taint.parameter_path,
                        matched_token=taint.matched_token,
                        reason=taint.reason,
                    )
                return ExecuteResult(allowed=False, explanation=explanation, decision=None)

        # [Phase 0 hotfix] Inject policy-driven resource limits into tool parameters.
        # Policy-derived constraints surfaced in `decision.details` (e.g. max_response_size)
        # are passed to the tool only when the caller has not already supplied them. This
        # prevents tools (e.g. http.fetch) from silently using their internal default cap
        # when the operator has set a stricter limit in the policy.
        parameters = self._apply_policy_resource_limits(parameters, decision)

        tool = get_tool(capability)
        if tool is None:
            denial_decision = Decision(
                allowed=False,
                reason=f"No tool registered for capability {capability!r}.",
                policy_rule=decision.policy_rule,
            )
            explanation = self.get_explanation(denial_decision, capability, parameters)
            logger.warning(explanation)

            if self.audit_logger:
                self.audit_logger.log_tool_execution(
                    capability=capability,
                    parameters=redacted_parameters,
                    success=False,
                    execution_time_ms=0,
                    error=redact_text(explanation),
                )

            return ExecuteResult(allowed=False, explanation=explanation, decision=denial_decision)

        # Optional: Docker sandbox for high-risk capabilities (Phase 5).
        use_docker_sandbox = os.environ.get("AGENT_RUNTIME_USE_DOCKER_SANDBOX", "").strip() == "1"
        if use_docker_sandbox and is_high_risk(capability):
            if not docker_available():
                explanation = "Sandbox required but Docker is unavailable."
                if self.audit_logger:
                    self.audit_logger.log_sandbox_execution(
                        capability=capability,
                        parameters=parameters,
                        success=False,
                        execution_time_ms=0,
                        sandbox={"type": "docker", "available": False},
                        error=explanation,
                    )
                return ExecuteResult(allowed=False, explanation=explanation, decision=decision)

            # Allow network only for http.fetch; keep others isolated.
            network = "bridge" if capability == "http.fetch" else "none"
            cfg = SandboxConfig(network=network)
            exec_start_time = datetime.now()
            tool_result = run_tool_in_docker(capability, parameters, cfg)
            execution_time_ms = (datetime.now() - exec_start_time).total_seconds() * 1000
            if self.audit_logger:
                self.audit_logger.log_sandbox_execution(
                    capability=capability,
                    parameters=parameters,
                    success=tool_result.success,
                    execution_time_ms=execution_time_ms,
                    sandbox={
                        "type": "docker",
                        "image": cfg.image,
                        "network": cfg.network,
                        "read_only": cfg.read_only,
                        "memory": cfg.memory,
                        "cpus": cfg.cpus,
                    },
                    result={"output_length": len(str(tool_result.output)) if tool_result.output else 0},
                    error=tool_result.error if not tool_result.success else None,
                )
            explanation = self._policy_engine.get_explanation(decision)
            return ExecuteResult(allowed=True, result=tool_result, explanation=explanation, decision=decision)

        # Execute tool with timing
        exec_start_time = datetime.now()
        try:
            raw_result = tool.execute(parameters)
            execution_time_ms = (datetime.now() - exec_start_time).total_seconds() * 1000
            redacted_output = redact_data(raw_result.output)
            redacted_error = redact_text(raw_result.error) if raw_result.error else None
            tool_result = ToolResult(success=raw_result.success, output=redacted_output, error=redacted_error)

            if self.audit_logger:
                self.audit_logger.log_tool_execution(
                    capability=capability,
                    parameters=redacted_parameters,
                    success=tool_result.success,
                    execution_time_ms=execution_time_ms,
                    result={
                        "output": redacted_output,
                        "output_length": len(str(redacted_output)) if redacted_output is not None else 0,
                        "success": tool_result.success,
                    },
                    error=redacted_error if not tool_result.success else None,
                )

            if not tool_result.success:
                denial_decision = Decision(
                    allowed=False,
                    reason=f"Tool execution failed: {redacted_error or 'unknown error'}",
                    policy_rule=decision.policy_rule,
                )
                explanation = self.get_explanation(denial_decision, capability, parameters)
                return ExecuteResult(
                    allowed=False,
                    result=tool_result,
                    explanation=explanation,
                    decision=denial_decision,
                )

            # [Phase 3] Register a successful tool output as a taint source if
            # this capability is configured as one. We register the *redacted*
            # output so secrets do not enter the taint store. This is the only
            # place outputs are recorded; failed executions do not produce
            # taint sources.
            if self._taint_tracker is not None and self._taint_tracker.is_source(capability):
                self._taint_tracker.register_source(capability, redacted_output)
        except ToolError as exc:
            execution_time_ms = (datetime.now() - exec_start_time).total_seconds() * 1000
            redacted_error = redact_text(str(exc))
            denial_decision = Decision(
                allowed=False,
                reason=f"Tool execution failed: {redacted_error}",
                policy_rule=decision.policy_rule,
            )
            explanation = self.get_explanation(denial_decision, capability, parameters)
            logger.exception("execute_tool ToolError: %s", capability)

            if self.audit_logger:
                self.audit_logger.log_tool_execution(
                    capability=capability,
                    parameters=redacted_parameters,
                    success=False,
                    execution_time_ms=execution_time_ms,
                    error=redacted_error,
                )

            return ExecuteResult(
                allowed=False,
                result=ToolResult(success=False, error=redacted_error),
                explanation=explanation,
                decision=denial_decision,
            )
        except Exception as exc:
            execution_time_ms = (datetime.now() - exec_start_time).total_seconds() * 1000
            redacted_error = redact_text(str(exc))
            denial_decision = Decision(
                allowed=False,
                reason=f"Tool execution failed: {redacted_error}",
                policy_rule=decision.policy_rule,
            )
            explanation = self.get_explanation(denial_decision, capability, parameters)
            logger.exception("execute_tool failed: %s", capability)

            if self.audit_logger:
                self.audit_logger.log_tool_execution(
                    capability=capability,
                    parameters=redacted_parameters,
                    success=False,
                    execution_time_ms=execution_time_ms,
                    error=redacted_error,
                )

            return ExecuteResult(
                allowed=False,
                result=ToolResult(success=False, error=redacted_error),
                explanation=explanation,
                decision=denial_decision,
            )

        explanation = self._policy_engine.get_explanation(decision)
        return ExecuteResult(
            allowed=True,
            result=tool_result,
            explanation=explanation,
            decision=decision,
        )


ARG_POLICY = "policy"
DEFAULT_POLICY = "configs/default_policy.yaml"

def main() -> None:
    """CLI entry: load policy, create runtime, optionally run one execute_tool (demo)."""
    import argparse
    import json
    from src.runtime.bootstrap import register_default_tools
    parser = argparse.ArgumentParser(description="Security-constrained agent runtime (plan §1.4)")
    parser.add_argument(
        "--" + ARG_POLICY,
        type=str,
        default=DEFAULT_POLICY,
        required=False,
        help=f"Path to policy (default: {DEFAULT_POLICY})"
    )
    parser.add_argument(
        "--capability",
        type=str,
        required=True,
        help="REQUIRED: The tool capability to execute"
    )
    parser.add_argument(
        "--params",
        type=str,
        default="{}",
        required=False,
        help="JSON params (default: {})"
    )
    parser.add_argument(
        "--approve",
        action="store_true",
        help="Auto-approve capabilities that require approval (demo only).",
    )
    parser.add_argument(
        "--prompt-approval",
        action="store_true",
        help="Prompt for approval when required (interactive).",
    )
    args = parser.parse_args()
    approval_cb = None
    if args.approve:
        approval_cb = lambda capability, params: True
    elif args.prompt_approval:
        def _prompt_cb(capability, params):
            ans = input(f"Approval required for {capability}. Approve? [y/N] ").strip().lower()
            return ans in ("y", "yes")
        approval_cb = _prompt_cb

    runtime = AgentRuntime(approval_callback=approval_cb)
    # policy_path = Path(args.policy) if args.policy else None
    # runtime.load_policy(policy_path)
    policy_path = Path(args.policy) if args.policy else Path(DEFAULT_POLICY)
    runtime.load_policy(policy_path)
    register_default_tools(runtime)
    if args.capability:
        params = json.loads(args.params)
        result = runtime.execute_tool(args.capability, params)
        print("allowed:", result.allowed)
        print("explanation:", result.explanation)
        if result.result:
            print("result:", result.result.to_dict())
    else:
        print("Runtime ready. Use --capability and --params to run a tool call, or use as a library.")
