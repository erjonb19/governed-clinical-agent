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
  of sent roots is per process and bounded (cleared at 10,000 traces): after a
  restart or a clear, a re-ended trace's outcome is the first root Phoenix got.
- an approval that is never decided sends no root at all: its steps appear in
  Phoenix without one, which is better than a root that says "pending" forever.

ERROR TEXT: an `error` attribute (and the span status message) is exported as
the exception's type only; its message can quote a data value. A step's
`reason` (a guard denial, a failed execute) is exported as recorded: it is the
same text the planner is shown on a retry, and an execution error is reduced to
its class and target type before it leaves the tool (describe_execution_error),
so it carries no value -- pinned on this path by
tests/integration/test_indirect_injection.py.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from urllib.parse import unquote
import threading
from typing import Any, Optional

ENDPOINT_ENV = "TRACE_OTLP_ENDPOINT"
PROJECT_ENV = "TRACE_OTLP_PROJECT"
# Auth for a hosted backend (backlog item 12), in the OpenTelemetry header format
# "key=value,key2=value2" -- e.g. Langfuse Cloud:
#   Authorization=Basic <base64 of public:secret key>,x-langfuse-ingestion-version=4
# A secret: set it in the host's environment (Render dashboard), never in code;
# it is sent only as request headers and never logged or recorded.
HEADERS_ENV = "TRACE_OTLP_HEADERS"
QUEUE_MAX = 2048                 # spans waiting for export; beyond this, new ones are dropped
EXPORT_TIMEOUT_SEC = 5
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
    """'ConversionException: Could not convert ...' -> 'ConversionException'.
    Anything that does not start with an exception-like name is just 'error',
    so a plain message containing a colon cannot leak its first part."""
    if not err:
        return ""
    head = str(err).split(":", 1)[0].strip()
    return head[:100] if re.fullmatch(r"[A-Za-z_][\w.]*", head) else "error"


_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def is_hosted(endpoint: str) -> bool:
    """Is the backend somewhere other than this machine? (Langfuse Cloud, any
    remote collector.) A local Phoenix gets the full recorded attributes; a
    hosted backend gets the reduced set (`hosted_attributes`)."""
    from urllib.parse import urlparse
    host = (urlparse(endpoint).hostname or "").lower()
    return host not in _LOCAL_HOSTS


def _mask_sql_literals(sql: str) -> str:
    # the existing literal redaction (the one used for SQL error messages):
    # every single-quoted literal becomes '<value>', so text a person typed
    # into the question cannot ride out inside a WHERE clause
    from analytics_query_tool import redact_values
    return redact_values(sql)


def hosted_attributes(name: str, attrs: dict) -> dict:
    """What a HOSTED backend receives (owner's decision 2026-11-07). The local
    SQLite traces and a local Phoenix keep everything; off this machine:
    - the visitor's question is replaced by its hash and length: the codebase
      has no free-text PHI redactor (the guard works on SQL), so the question
      is withheld until one is approved;
    - SQL string literals are masked;
    - an approval decision's reason and the reviewer identity, and every
      escalation target, are dropped."""
    out = dict(attrs)
    q = out.pop("question", None)
    if q:
        out["question_sha256"] = hashlib.sha256(str(q).encode("utf-8")).hexdigest()[:16]
        out["question_chars"] = len(str(q))
    if out.get("sql"):
        out["sql"] = _mask_sql_literals(str(out["sql"]))
    out.pop("escalated_to", None)
    if name == "approval.decision":
        out.pop("reason", None)
        out.pop("decided_by", None)
    return out


def otel_attributes(name: str, attrs: dict, hosted: bool = False) -> dict:
    """Recorded attrs as OTel attributes, plus the OpenInference fields Phoenix
    reads. `attrs` must already be scrubbed. `error` keeps its type only; with
    `hosted`, the reduced set from `hosted_attributes`."""
    attrs = hosted_attributes(name, attrs) if hosted else dict(attrs)
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
                 processor=None, headers: Optional[dict] = None,
                 timeout_sec: float = EXPORT_TIMEOUT_SEC):
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        self.endpoint = endpoint
        self.hosted = is_hosted(endpoint)
        self.resource = Resource.create({"service.name": project,
                                         "openinference.project.name": project})
        if span_exporter is None:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            span_exporter = OTLPSpanExporter(endpoint=endpoint, headers=headers or None,
                                             timeout=timeout_sec)
        # Export runs on the processor's own thread from a bounded queue: a slow,
        # failing or unreachable backend delays no request, and when the queue is
        # full new spans are dropped rather than held (tests/unit/test_trace_export.py)
        self.processor = processor or BatchSpanProcessor(
            span_exporter, max_queue_size=QUEUE_MAX, schedule_delay_millis=500,
            export_timeout_millis=int(timeout_sec * 1000))
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
                            attributes=otel_attributes(name, attrs, self.hosted), status=st,
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


def parse_headers(value: str) -> dict:
    """'k=v,k2=v2' (the OTEL_EXPORTER_OTLP_HEADERS format) -> {'k': 'v', ...}.
    Splits each pair on its FIRST '=', so base64 padding survives; values are
    percent-decoded as the OpenTelemetry spec says. Malformed pairs are skipped."""
    out = {}
    for pair in (value or "").split(","):
        if "=" not in pair:
            continue
        k, v = pair.split("=", 1)
        k = k.strip()
        if k:
            out[k] = unquote(v.strip())
    return out


def from_env() -> Optional[OTLPExporter]:
    """The exporter for TRACE_OTLP_ENDPOINT (with TRACE_OTLP_HEADERS), built
    once per configuration; None when unset or when the OpenTelemetry packages
    are missing."""
    endpoint = os.environ.get(ENDPOINT_ENV, "").strip()
    if not endpoint:
        return None
    project = os.environ.get(PROJECT_ENV, "").strip() or DEFAULT_PROJECT
    headers = parse_headers(os.environ.get(HEADERS_ENV, ""))
    key = (endpoint, project, hashlib.sha256(repr(sorted(headers.items())).encode()).hexdigest())
    with _lock:
        if key not in _cached:
            try:
                _cached[key] = OTLPExporter(endpoint, project, headers=headers)
            except Exception:
                _cached[key] = None
        return _cached[key]
