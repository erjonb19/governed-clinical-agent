"""Audit trail logging for security decisions and operations."""

import json
import os
import logging
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Optional, List
from threading import Lock
import hashlib

from src.runtime import request_context
from src.utils.redaction import redact_data, redact_text

import phi_redact  # item 23: free-text identifiers out of every audit line


# Item 63: a failed audit write is never silent. Every best-effort audit write
# (the answer event, the critic's review, a refused key) reports here: one
# warning with the exception TYPE only (its message can carry a path or a value)
# and one process-wide count that /metrics shows.
_failure_log = logging.getLogger("governed.audit")
_failure_lock = Lock()
_failure_count = 0


def record_failure(where: str, exc: BaseException, events: int = 1) -> None:
    """Count `events` lost audit writes (a batch counts each of its events)."""
    global _failure_count
    with _failure_lock:
        _failure_count += max(1, events)
    if events > 1:
        _failure_log.warning("audit write failed (%s, %d events): %s", where, events,
                             type(exc).__name__)
    else:
        _failure_log.warning("audit write failed (%s): %s", where, type(exc).__name__)


def failure_count() -> int:
    with _failure_lock:
        return _failure_count


def reset_failures() -> None:
    global _failure_count
    with _failure_lock:
        _failure_count = 0


class AuditEventType(Enum):
    """Types of audit events."""
    POLICY_EVALUATION = "policy_evaluation"
    TOOL_EXECUTION = "tool_execution"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_DECISION = "approval_decision"
    INJECTION_DETECTED = "injection_detected"
    PARAMETER_VALIDATION = "parameter_validation"
    TAINT_VIOLATION = "taint_violation"
    SANDBOX_EXECUTION = "sandbox_execution"
    CRITIC_REVIEW = "critic_review"
    ANSWER = "answer"
    AUTH_FAILURE = "auth_failure"


class DecisionType(Enum):
    """Security decision outcomes."""
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


@dataclass
class AuditEvent:
    """Represents a single audit event."""
    timestamp: str
    event_type: AuditEventType
    agent_id: str
    capability: str
    decision: DecisionType
    reason: str
    
    # Optional fields
    parameters: Optional[Dict[str, Any]] = None
    policy_rule: Optional[str] = None
    execution_result: Optional[Dict[str, Any]] = None
    performance_metrics: Optional[Dict[str, float]] = None
    context: Optional[Dict[str, Any]] = None
    # Who asked and from where (backlog item 38): filled from the HTTP request
    # being served (src/runtime/request_context.py); None outside a request.
    request_id: Optional[str] = field(
        default_factory=lambda: request_context.current().get("request_id"))
    principal: Optional[str] = field(
        default_factory=lambda: request_context.current().get("principal"))
    source_ip: Optional[str] = field(
        default_factory=lambda: request_context.current().get("source_ip"))
    event_id: str = field(default_factory=lambda: hashlib.sha256(
        f"{time.time()}{id(object())}".encode()
    ).hexdigest()[:16])
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        data = asdict(self)
        # Convert enums to strings
        data['event_type'] = self.event_type.value
        data['decision'] = self.decision.value
        # Secrets (redaction.py), then personal identifiers in free text
        # (phi_redact, backlog item 23): a reason or a context note can carry
        # model text that quotes the visitor's question. SQL parameters are
        # left as SQL; result rows never reach an audit line (item 59).
        data['reason'] = _phi_text(redact_text(data.get('reason', '')))
        if self.parameters is not None:
            data['parameters'] = redact_data(self.parameters)
        if self.execution_result is not None:
            data['execution_result'] = redact_data(self.execution_result)
        if self.context is not None:
            data['context'] = _phi_free_text(redact_data(self.context))
        return data


# Context fields that hold free text a person or a model wrote. Everything else
# in a context is a system value (trace ids are UUIDs, commits are hex, counts are
# numbers) that the redactor would mangle and that holds nothing personal.
FREE_TEXT_KEYS = frozenset({"comment", "reason", "error", "pattern_matched", "issues"})


def _phi_text(text: Any) -> Any:
    return phi_redact.redact(text)[0] if isinstance(text, str) and text else text


def _phi_free_text(context: Any) -> Any:
    if not isinstance(context, dict):
        return context
    out = {}
    for k, v in context.items():
        if k in FREE_TEXT_KEYS:
            out[k] = [_phi_text(x) for x in v] if isinstance(v, list) else _phi_text(v)
        else:
            out[k] = v
    return out


class AuditLogger:
    """
    Comprehensive audit logging system for security events.
    
    Features:
    - Structured JSON logging
    - Sensitive data redaction
    - Asynchronous writes (via buffering)
    - Query and analysis support
    - Thread-safe operations
    - Performance metrics tracking
    """
    
    def __init__(
        self,
        log_dir: Path,
        agent_id: str,
        max_buffer_size: int = 100,
        enable_console: bool = True
    ):
        """
        Initialize the audit logger.
        
        Args:
            log_dir: Directory to store audit logs
            agent_id: Identifier for the agent
            max_buffer_size: Number of events to buffer before flushing
            enable_console: Whether to also log to console
        """
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.agent_id = agent_id
        self.max_buffer_size = max_buffer_size
        
        # Create log file with timestamp
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_file = self.log_dir / f"audit_{agent_id}_{timestamp}.jsonl"
        
        # Event buffer for async writes
        self._buffer: List[AuditEvent] = []
        self._buffer_lock = Lock()
        
        # Configure Python logger for console output
        self._console_logger = None
        if enable_console:
            self._console_logger = logging.getLogger(f"audit.{agent_id}")
            self._console_logger.setLevel(logging.INFO)
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter(
                '%(asctime)s - AUDIT - %(message)s'
            ))
            self._console_logger.addHandler(handler)
        
        # Statistics
        self._stats = {
            'total_events': 0,
            'allow_count': 0,
            'deny_count': 0,
            'approval_count': 0,
            'injection_count': 0,
        }
        self._stats_lock = Lock()

        # Item 39: with STATE_DB_URL (Postgres) every event also goes to the
        # append-only `audit_events` table, through one background writer per
        # process (audit_store.py). None: the JSONL is the only record.
        self._store_writer = None
        try:
            import audit_store

            self._store_writer = audit_store.writer()
        except Exception as e:  # noqa: BLE001 -- the JSONL still records
            record_failure("audit_store_init", e)
    
    def log_policy_evaluation(
        self,
        capability: str,
        decision: DecisionType,
        reason: str,
        parameters: Optional[Dict[str, Any]] = None,
        policy_rule: Optional[str] = None,
        evaluation_time_ms: Optional[float] = None,
        context: Optional[Dict[str, Any]] = None
    ) -> str:
        """
        Log a policy evaluation event.

        `context` carries operator-only detail, e.g. the least-privilege
        `policy_snippet` for a denial -- recorded here and never returned to
        the caller.
        
        Args:
            capability: The capability being evaluated
            decision: The policy decision (allow/deny/require_approval)
            reason: Explanation for the decision
            parameters: Parameters of the request
            policy_rule: The specific policy rule that was applied
            evaluation_time_ms: Time taken to evaluate (milliseconds)
        
        Returns:
            Event ID for correlation
        """
        performance_metrics = {}
        if evaluation_time_ms is not None:
            performance_metrics['evaluation_time_ms'] = evaluation_time_ms
        
        event = AuditEvent(
            timestamp=datetime.now().isoformat(),
            event_type=AuditEventType.POLICY_EVALUATION,
            agent_id=self.agent_id,
            capability=capability,
            decision=decision,
            reason=reason,
            parameters=parameters,
            policy_rule=policy_rule,
            performance_metrics=performance_metrics,
            context=context
        )

        return self._log_event(event)
    
    def log_tool_execution(
        self,
        capability: str,
        parameters: Dict[str, Any],
        success: bool,
        execution_time_ms: float,
        result: Optional[Any] = None,
        error: Optional[str] = None,
        resource_usage: Optional[Dict[str, float]] = None
    ) -> str:
        """
        Log a tool execution event.
        
        Args:
            capability: The tool capability executed
            parameters: Parameters passed to the tool
            success: Whether execution succeeded
            execution_time_ms: Time taken to execute
            result: Execution result (will be redacted if sensitive)
            error: Error message if execution failed
            resource_usage: CPU, memory, network usage
        
        Returns:
            Event ID for correlation
        """
        execution_result = {
            'success': success,
            'result': result,
            'error': error
        }
        
        performance_metrics = {
            'execution_time_ms': execution_time_ms
        }
        if resource_usage:
            performance_metrics.update(resource_usage)
        
        decision = DecisionType.ALLOW if success else DecisionType.DENY
        reason = "Execution completed" if success else f"Execution failed: {error}"
        
        event = AuditEvent(
            timestamp=datetime.now().isoformat(),
            event_type=AuditEventType.TOOL_EXECUTION,
            agent_id=self.agent_id,
            capability=capability,
            decision=decision,
            reason=reason,
            parameters=parameters,
            execution_result=execution_result,
            performance_metrics=performance_metrics
        )
        
        return self._log_event(event)
    
    def log_injection_detected(
        self,
        capability: str,
        parameters: Dict[str, Any],
        injection_type: str,
        pattern_matched: str,
        context: Optional[Dict[str, Any]] = None
    ) -> str:
        """
        Log an injection detection event.
        
        Args:
            capability: The capability where injection was detected
            parameters: Parameters containing the injection
            injection_type: Type of injection (prompt, output, command, etc.)
            pattern_matched: The pattern that triggered detection
            context: Additional context about the detection
        
        Returns:
            Event ID for correlation
        """
        event = AuditEvent(
            timestamp=datetime.now().isoformat(),
            event_type=AuditEventType.INJECTION_DETECTED,
            agent_id=self.agent_id,
            capability=capability,
            decision=DecisionType.DENY,
            reason=f"Injection detected: {injection_type}",
            parameters=parameters,
            context={
                'injection_type': injection_type,
                'pattern_matched': pattern_matched,
                **(context or {})
            }
        )
        
        with self._stats_lock:
            self._stats['injection_count'] += 1
        
        return self._log_event(event)
    
    def log_approval_requested(
        self,
        capability: str,
        parameters: Dict[str, Any],
        reason: str,
        timeout_seconds: Optional[int] = None
    ) -> str:
        """
        Log an approval request event.
        
        Args:
            capability: The capability requiring approval
            parameters: Parameters of the request
            reason: Why approval is required
            timeout_seconds: Approval timeout
        
        Returns:
            Event ID for correlation
        """
        context = {}
        if timeout_seconds:
            context['timeout_seconds'] = timeout_seconds
        
        event = AuditEvent(
            timestamp=datetime.now().isoformat(),
            event_type=AuditEventType.APPROVAL_REQUESTED,
            agent_id=self.agent_id,
            capability=capability,
            decision=DecisionType.REQUIRE_APPROVAL,
            reason=reason,
            parameters=parameters,
            context=context
        )
        
        with self._stats_lock:
            self._stats['approval_count'] += 1
        
        return self._log_event(event)
    
    def log_approval_decision(
        self,
        request_event_id: str,
        capability: str,
        approved: bool,
        approver: str,
        comment: Optional[str] = None
    ) -> str:
        """
        Log an approval decision event.
        
        Args:
            request_event_id: Event ID of the original approval request
            capability: The capability that was approved/denied
            approved: Whether the request was approved
            approver: Who made the approval decision
            comment: Optional comment from approver
        
        Returns:
            Event ID for correlation
        """
        decision = DecisionType.ALLOW if approved else DecisionType.DENY
        reason = f"{'Approved' if approved else 'Denied'} by {approver}"
        if comment:
            reason += f": {comment}"
        
        event = AuditEvent(
            timestamp=datetime.now().isoformat(),
            event_type=AuditEventType.APPROVAL_DECISION,
            agent_id=self.agent_id,
            capability=capability,
            decision=decision,
            reason=reason,
            context={
                'request_event_id': request_event_id,
                'approver': approver,
                'comment': comment
            }
        )
        
        return self._log_event(event)
    
    def log_parameter_validation(
        self,
        capability: str,
        parameters: Dict[str, Any],
        validation_errors: List[str],
        constraint_violated: Optional[str] = None
    ) -> str:
        """
        Log a parameter validation failure.
        
        Args:
            capability: The capability being validated
            parameters: Parameters that failed validation
            validation_errors: List of validation error messages
            constraint_violated: Specific constraint that was violated
        
        Returns:
            Event ID for correlation
        """
        event = AuditEvent(
            timestamp=datetime.now().isoformat(),
            event_type=AuditEventType.PARAMETER_VALIDATION,
            agent_id=self.agent_id,
            capability=capability,
            decision=DecisionType.DENY,
            reason=f"Parameter validation failed: {'; '.join(validation_errors)}",
            parameters=parameters,
            context={
                'validation_errors': validation_errors,
                'constraint_violated': constraint_violated
            }
        )
        
        return self._log_event(event)

    def log_taint_violation(
        self,
        capability: str,
        parameters: Dict[str, Any],
        source_capability: str,
        source_id: str,
        parameter_path: str,
        matched_token: str,
        reason: str,
    ) -> str:
        """
        Log a taint-flow violation: data from an untrusted source was used as a
        parameter to a high-risk sink capability.

        Args:
            capability: The sink capability that was about to execute.
            parameters: The parameters that contained the tainted value.
            source_capability: The capability that produced the original tainted output.
            source_id: Short hash identifying the upstream source for correlation.
            parameter_path: Dotted path of the offending parameter (e.g. "url" or "files[0].body").
            matched_token: The actual substring that was found in both source and sink (truncated).
            reason: Human-readable explanation of the violation.

        Returns:
            Event ID for correlation.
        """
        event = AuditEvent(
            timestamp=datetime.now().isoformat(),
            event_type=AuditEventType.TAINT_VIOLATION,
            agent_id=self.agent_id,
            capability=capability,
            decision=DecisionType.DENY,
            reason=reason,
            parameters=parameters,
            context={
                "source_capability": source_capability,
                "source_id": source_id,
                "parameter_path": parameter_path,
                "matched_token": matched_token,
            },
        )
        return self._log_event(event)

    def log_critic_review(
        self,
        capability: str,
        verdict: str,
        reason: str,
        sql_sha256: Optional[str] = None
    ) -> str:
        """
        Log the critic's verdict on a proposed query (PLAN v2 item 4).

        The critic is a quality step, not a control: "revise" sends the agent
        back to re-plan, and "approve" or "unavailable" lets the query go on to
        the guard, which still decides what runs. So approve and unavailable are
        recorded as ALLOW, revise as DENY, and the verdict itself is in context.

        Args:
            capability: The capability the query was proposed for
            verdict: approve | revise | unavailable
            reason: The critic's one-line reason
            sql_sha256: Hash of the reviewed SQL (the SQL itself is in the trace)

        Returns:
            Event ID for correlation
        """
        event = AuditEvent(
            timestamp=datetime.now().isoformat(),
            event_type=AuditEventType.CRITIC_REVIEW,
            agent_id=self.agent_id,
            capability=capability,
            decision=DecisionType.DENY if verdict == "revise" else DecisionType.ALLOW,
            reason=reason,
            context={"verdict": verdict, "sql_sha256": sql_sha256}
        )
        return self._log_event(event)

    def log_answer(
        self,
        capability: str,
        allowed: bool,
        decided_by: Optional[str],
        version: Dict[str, Any],
        trace_id: Optional[str] = None,
    ) -> str:
        """
        Log which version of the agent produced an answer (backlog item 4):
        prompt_version, model, provider and git_commit, with the outcome and
        the trace id. No question text, SQL or rows -- the trace has the SQL.
        """
        event = AuditEvent(
            timestamp=datetime.now().isoformat(),
            event_type=AuditEventType.ANSWER,
            agent_id=self.agent_id,
            capability=capability,
            decision=DecisionType.ALLOW if allowed else DecisionType.DENY,
            reason=f"answer: {decided_by}",
            context={**{k: version.get(k) for k in
                        ("prompt_version", "model", "provider", "git_commit")},
                     "trace_id": trace_id, "decided_by": decided_by},
        )
        return self._log_event(event)

    def log_auth_failure(
        self,
        client: str,
        path: str,
        surface: str,
        failures_in_window: int,
        throttled: bool,
    ) -> str:
        """
        Log a refused API key (backlog item 35): who (client address), where,
        and how many failures that client has in the window. Never the key.
        """
        event = AuditEvent(
            timestamp=datetime.now().isoformat(),
            event_type=AuditEventType.AUTH_FAILURE,
            agent_id=self.agent_id,
            capability=f"auth.{surface}",
            decision=DecisionType.DENY,
            reason="throttled: too many failed keys" if throttled else "invalid or missing API key",
            context={"client": client, "path": path, "surface": surface,
                     "failures_in_window": failures_in_window, "throttled": throttled},
        )
        return self._log_event(event)

    def log_sandbox_execution(
        self,
        capability: str,
        parameters: Dict[str, Any],
        success: bool,
        execution_time_ms: float,
        sandbox: Dict[str, Any],
        result: Optional[Any] = None,
        error: Optional[str] = None,
    ) -> str:
        """
        Log a sandboxed execution event (Docker sandbox, etc.).

        This complements TOOL_EXECUTION by recording the sandbox configuration
        and the fact that execution occurred inside an isolation boundary.
        """
        execution_result = {
            "success": success,
            "result": result,
            "error": error,
        }
        performance_metrics = {
            "execution_time_ms": execution_time_ms,
        }
        decision = DecisionType.ALLOW if success else DecisionType.DENY
        reason = "Sandbox execution completed" if success else f"Sandbox execution failed: {error}"

        event = AuditEvent(
            timestamp=datetime.now().isoformat(),
            event_type=AuditEventType.SANDBOX_EXECUTION,
            agent_id=self.agent_id,
            capability=capability,
            decision=decision,
            reason=reason,
            parameters=parameters,
            execution_result=execution_result,
            performance_metrics=performance_metrics,
            context={"sandbox": sandbox},
        )
        return self._log_event(event)
    
    def _log_event(self, event: AuditEvent) -> str:
        """
        Internal method to log an event.
        
        Args:
            event: The audit event to log
        
        Returns:
            Event ID
        """
        # Update statistics
        with self._stats_lock:
            self._stats['total_events'] += 1
            if event.decision == DecisionType.ALLOW:
                self._stats['allow_count'] += 1
            elif event.decision == DecisionType.DENY:
                self._stats['deny_count'] += 1
        
        # Console logging
        if self._console_logger:
            log_msg = (
                f"{event.event_type.value} | "
                f"{event.capability} | "
                f"{event.decision.value} | "
                f"{event.reason}"
            )
            if event.decision == DecisionType.DENY:
                self._console_logger.warning(log_msg)
            else:
                self._console_logger.info(log_msg)
        
        # Add to buffer
        with self._buffer_lock:
            self._buffer.append(event)
            if len(self._buffer) >= self.max_buffer_size:
                self._flush_buffer()
        
        return event.event_id
    
    def _rotate(self) -> None:
        """Backlog item 60: cap the audit JSONL. Past AUDIT_LOG_MAX_BYTES the
        current file is closed and a new one started; only the newest
        AUDIT_LOG_KEEP files of this agent are kept (0 keeps everything). Until
        audit events reach an append-only store (item 39), this file is the
        record, so the defaults are generous: 10 MB x 20 files."""
        try:
            max_bytes = int(os.environ.get("AUDIT_LOG_MAX_BYTES", 10_000_000))
            keep = int(os.environ.get("AUDIT_LOG_KEEP", 20))
        except ValueError:
            max_bytes, keep = 10_000_000, 20
        if max_bytes > 0 and self.log_file.exists() and self.log_file.stat().st_size >= max_bytes:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            self.log_file = self.log_dir / f"audit_{self.agent_id}_{stamp}.jsonl"
        self._keep = keep

    def _prune(self) -> None:
        """Drop this agent's oldest audit files beyond the newest `_keep`
        (after a write, so the current file is counted)."""
        keep = getattr(self, "_keep", 0)
        if keep <= 0:
            return
        mine = sorted(self.log_dir.glob(f"audit_{self.agent_id}_*.jsonl"),
                      key=lambda p: (p.stat().st_mtime_ns, p.name))
        for old in mine[:-keep]:
            if old != self.log_file:
                try:
                    old.unlink()
                except OSError:
                    pass

    def _flush_buffer(self):
        """Flush buffered events to disk."""
        if not self._buffer:
            return
        self._rotate()

        rows = [event.to_dict() for event in self._buffer]
        with self.log_file.open('a') as f:
            for row in rows:
                json.dump(row, f)
                f.write('\n')
        if self._store_writer is not None:
            for row in rows:
                self._store_writer.put(row)
        
        self._buffer.clear()
        self._prune()
    
    def flush(self):
        """Manually flush the buffer to disk."""
        with self._buffer_lock:
            self._flush_buffer()
    
    def get_statistics(self) -> Dict[str, int]:
        """Get current logging statistics."""
        with self._stats_lock:
            return self._stats.copy()
    
    def query_events(
        self,
        event_type: Optional[AuditEventType] = None,
        capability: Optional[str] = None,
        decision: Optional[DecisionType] = None,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        limit: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        Query audit events from the log file.
        
        Args:
            event_type: Filter by event type
            capability: Filter by capability
            decision: Filter by decision type
            start_time: Filter by start timestamp (ISO format)
            end_time: Filter by end timestamp (ISO format)
            limit: Maximum number of events to return
        
        Returns:
            List of matching events
        """
        # Flush buffer first to ensure we read all events
        self.flush()
        
        events = []
        with self.log_file.open('r') as f:
            for line in f:
                event = json.loads(line)
                
                # Apply filters
                if event_type and event['event_type'] != event_type.value:
                    continue
                if capability and event['capability'] != capability:
                    continue
                if decision and event['decision'] != decision.value:
                    continue
                if start_time and event['timestamp'] < start_time:
                    continue
                if end_time and event['timestamp'] > end_time:
                    continue
                
                events.append(event)
                
                if limit and len(events) >= limit:
                    break
        
        return events
    
    def __enter__(self):
        """Context manager entry."""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - ensure buffer is flushed."""
        self.flush()


class RowlessAuditLogger(AuditLogger):
    """Records that a query ran, never what it returned.

    `redact_data` masks secrets, not data values, so a FHIR query's rows would
    otherwise land in the file verbatim. Drops `rows` from a tool's output;
    row_count and columns stay. Used by the standalone MCP server (backlog
    item 52); the API's `_ServiceAuditLogger` does the same.
    """

    def log_tool_execution(self, capability, parameters, success, execution_time_ms,
                           result=None, error=None, resource_usage=None):
        out = result.get("output") if isinstance(result, dict) else None
        if isinstance(out, dict) and "rows" in out:
            result = {**result, "output": {k: v for k, v in out.items() if k != "rows"}}
        return super().log_tool_execution(capability, parameters, success, execution_time_ms,
                                          result, error, resource_usage)
