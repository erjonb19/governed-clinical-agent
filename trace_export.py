"""Mirror the run traces to a self-hosted OpenTelemetry backend (Arize Phoenix).

Backlog item 2. `tracing.py` already records one trace per run and one span
per step in SQLite, which the UI's Traces view draws. This module also sends
each finished span, over OTLP/HTTP, to a trace backend YOU run -- Arize Phoenix
by default (`phoenix serve`, http://localhost:6006). It is off unless
`TRACE_OTLP_ENDPOINT` is set, so the deploy and the tests send nothing.

    TRACE_OTLP_ENDPOINT=http://localhost:6006/v1/traces   # Phoenix's OTLP receiver
    TRACE_OTLP_PROJECT=governed-clinical-agent            # the Phoenix project (default)

WHAT IS SENT: exactly what the SQLite store keeps -- the question, the SQL, its
hash, row COUNTS, provider/model, guard decisions -- after the same `_scrub`
(no `rows` / `result` keys, bounded strings). Result rows never leave the
process (CLAUDE.md rule 3). Point it only at a backend you control.

HOW: spans are rebuilt from the recorded values (`ReadableSpan` with the
recorded start and end times and ids) and queued on a `BatchSpanProcessor`,
which exports from its own thread. So a slow or absent backend never delays a
request, and like the tracer itself, this never raises into one.

IDS: the trace id and span ids are the tracer's own (16 hex chars). A run's
top-level spans are children of a synthesized `run` root span whose id is
derived from the trace id, so an approval decided hours later (`attach`) still
lands under the same trace in Phoenix.

ONE ROOT PER TRACE: Phoenix keeps the FIRST span it receives for an id (tested
against arize-phoenix 21.0.0), so the root is sent once, at a final status:
- `pending_approval` is not final; the root goes when the decision ends the trace.
- a trace that ends again after its root was sent (a failed run resumed) gets a
  child `<name>.end` span with the new outcome, never a second root. The record
  of sent roots is per process: after a restart, a re-ended trace's outcome is
  the first root Phoenix received.

ERROR TEXT: an `error` attribute (and the span status message) is exported as
the exception's type only; its message can quote a data value.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from typing import Any, Optional

ENDPOINT_ENV = "TRACE_OTLP_ENDPOINT"
PROJECT_ENV = "TRACE_OTLP_PROJECT"
DEFAULT_PROJECT = "governed-clinical-agent"
NON_FINAL = {"pending_approval"}

# OpenInference span kinds, so Phoenix shows each step with the right icon and
# lets you filter by kind. Unlisted names are CHAIN.
SPAN_KINDS = {
    "run": "CHAIN",
    "plan": "LLM",
    "critic": "LLM",
    "guard": "GUARDRAIL",
    "execute": "TOOL",
    "route": "CHAIN",
    "retry": "CHAIN",
    "answer": "CHAIN",
    "resume": "CHAIN",
    "approval.propose": "CHAIN",
    "approval.decision": "CHAIN",
}


def _int_id(value: str, bits: int) -> int:
    """A tracer id as an OTel id: the hex value when it is hex, else a hash.
    Never 0 (OTel treats 0 as invalid)."""
    try:
        n = int(value, 16)
        if 0 < n < (1 << bits):
            return n
    except (TypeError, ValueError):
        pass
    h = int(hashlib.sha256(str(value).encode("utf-8")).hexdigest(), 16) & ((1 << bits) - 1)
    return h or 1


def root_span_id(trace_id: str) -> int:
    """The synthesized `run` span's id, the same every time for one trace."""
    return _int_id(hashlib.sha256(f"run:{trace_id}".encode("utf-8")).hexdigest()[:16], 64)


def _attr_value(v: Any) -> Any:
    if isinstance(v, (str, bool, int, float)):
        return v
    if v is None:
        return None
    return json.dumps(v, default=str, separators=(",", ":"))


def error_type(err: Any) -> str:
    """'ConversionException: Could not convert ...' -> 'ConversionException'."""
    return str(err).split(":", 1)[0].strip()[:100] if err else ""


def otel_attributes(name: str, attrs: dict) -> dict:
    """Recorded attrs as OTel attributes, plus the OpenInference fields Phoenix
    reads. `attrs` must already be scrubbed. `error` keeps its type only."""
    attrs = dict(attrs)
    if attrs.get("error"):
        attrs["error"] = error_type(attrs["error"])
    out = {f"agent.{k}": _attr_value(v) for k, v in attrs.items()}
    out = {k: v for k, v in out.items() if v is not None}
    out["openinference.span.kind"] = SPAN_KINDS.get(name, "CHAIN")
    if attrs.get("question"):
        out["input.value"] = str(attrs["question"])
    if attrs.get("sql"):
        out["output.value"] = str(attrs["sql"])
    if attrs.get("model"):
        out["llm.model_name"] = str(attrs["model"])
    if attrs.get("provider"):
        out["llm.provider"] = str(attrs["provider"])
    return out


class OTLPExporter:
    """Queues recorded spans for export. Every public method is best effort."""

    def __init__(self, endpoint: str, project: str = DEFAULT_PROJECT, span_exporter=None,
                 processor=None):
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        self.endpoint = endpoint
        self.resource = Resource.create({"service.name": project,
                                         "openinference.project.name": project})
        if span_exporter is None:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            span_exporter = OTLPSpanExporter(endpoint=endpoint, timeout=5)
        self.processor = processor or BatchSpanProcessor(span_exporter, schedule_delay_millis=500)
        self._rooted: set = set()          # traces whose root this process has sent
        self._rooted_lock = threading.Lock()

    def _send(self, name: str, trace_id: str, span_id: int, parent_id: Optional[int],
              start_ts: float, end_ts: float, status: str, attrs: dict) -> None:
        from opentelemetry.sdk.trace import ReadableSpan
        from opentelemetry.trace import SpanContext, TraceFlags
        from opentelemetry.trace.status import Status, StatusCode

        tid = _int_id(trace_id, 128)
        ctx = SpanContext(trace_id=tid, span_id=span_id, is_remote=False,
                          trace_flags=TraceFlags(TraceFlags.SAMPLED))
        parent = (SpanContext(trace_id=tid, span_id=parent_id, is_remote=False,
                              trace_flags=TraceFlags(TraceFlags.SAMPLED))
                  if parent_id else None)
        st = Status(StatusCode.ERROR, error_type(attrs.get("error"))) if status == "error" \
            else Status(StatusCode.OK)
        span = ReadableSpan(name=name, context=ctx, parent=parent, resource=self.resource,
                            attributes=otel_attributes(name, attrs), status=st,
                            start_time=int(start_ts * 1e9), end_time=int(end_ts * 1e9))
        self.processor.on_end(span)

    def span(self, *, trace_id: str, span_id: str, parent_id: Optional[str], name: str,
             start_ts: float, end_ts: float, status: str, attrs: dict) -> None:
        try:
            parent = _int_id(parent_id, 64) if parent_id else root_span_id(trace_id)
            self._send(name, trace_id, _int_id(span_id, 64), parent, start_ts, end_ts, status,
                       attrs)
        except Exception:
            pass

    def root(self, *, trace_id: str, name: str, start_ts: float, end_ts: float, status: str,
             attrs: dict) -> None:
        """The trace's root, once, at a final status (see ONE ROOT PER TRACE)."""
        try:
            if status in NON_FINAL:
                return
            with self._rooted_lock:
                again = trace_id in self._rooted
                self._rooted.add(trace_id)
                if len(self._rooted) > 10000:      # bounded; oldest order is not needed
                    self._rooted.clear()
                    self._rooted.add(trace_id)
            if again:
                span_id = _int_id(hashlib.sha256(f"end:{trace_id}:{end_ts}".encode("utf-8"))
                                  .hexdigest()[:16], 64)
                self._send(f"{name}.end", trace_id, span_id, root_span_id(trace_id), end_ts,
                           end_ts, status, attrs)
            else:
                self._send(name, trace_id, root_span_id(trace_id), None, start_ts, end_ts,
                           status, attrs)
        except Exception:
            pass

    def flush(self, timeout_millis: int = 5000) -> bool:
        try:
            return bool(self.processor.force_flush(timeout_millis))
        except Exception:
            return False

    def shutdown(self) -> None:
        try:
            self.processor.shutdown()
        except Exception:
            pass


_lock = threading.Lock()
_cached: dict = {}


def from_env() -> Optional[OTLPExporter]:
    """The exporter for TRACE_OTLP_ENDPOINT, built once per endpoint; None when
    unset or when the OpenTelemetry packages are missing."""
    endpoint = os.environ.get(ENDPOINT_ENV, "").strip()
    if not endpoint:
        return None
    project = os.environ.get(PROJECT_ENV, "").strip() or DEFAULT_PROJECT
    key = (endpoint, project)
    with _lock:
        if key not in _cached:
            try:
                _cached[key] = OTLPExporter(endpoint, project)
            except Exception:
                _cached[key] = None
        return _cached[key]
