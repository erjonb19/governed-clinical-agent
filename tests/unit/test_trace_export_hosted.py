"""Exporting to a HOSTED backend (backlog item 12: Render -> Langfuse Cloud).

The real OTLP/HTTP exporter and BatchSpanProcessor are used here, against small
local HTTP servers that behave like a hosted backend that is down, refusing,
rejecting the key, or hanging. The owner's requirement: the exporter fails
quietly -- a request never raises and is never slowed by the backend -- and the
local SQLite traces are written regardless. Also: the auth header is sent, and
never recorded or logged.
"""

import http.server
import logging
import os
import socket
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

pytest.importorskip("opentelemetry.exporter.otlp.proto.http")

import trace_export  # noqa: E402
import tracing  # noqa: E402

SECRET = "Basic cGstbGYtZmFrZTpzay1sZi1mYWtl"      # base64 of a fake pk:sk pair


class _Backend:
    """A local stand-in for the hosted collector."""

    def __init__(self, status=200, delay=0.0):
        seen = self.requests = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                seen.append(dict(self.headers))
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                time.sleep(delay)
                self.send_response(status)
                self.end_headers()

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/api/public/otel/v1/traces"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


def _closed_port_url():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()                                       # nothing listens here now
    return f"http://127.0.0.1:{port}/v1/traces"


def _run_requests(tmp_path, endpoint, n=300, headers=None):
    """n traced 'requests' (4 spans each: root, plan, route, and the trace end)
    through a real exporter. Returns (seconds, tracer, exporter)."""
    # 1 s export timeout (the default is 5) keeps the shutdown of a failing
    # exporter short in tests; the behaviour under test does not depend on it
    exp = trace_export.OTLPExporter(endpoint, headers=headers, timeout_sec=1.0)
    t = tracing.Tracer(str(tmp_path / "t.sqlite"), exporter=exp)
    start = time.perf_counter()
    for i in range(n):
        with t.trace("run", question=f"q{i}"):
            with t.span("plan", sql="SELECT 1"):
                pass
            t.event("route", source="critic", destination="execute")
    return time.perf_counter() - start, t, exp


@pytest.mark.parametrize("backend", ["down", "503", "401 bad key", "hangs 3 s"])
def test_a_failing_hosted_backend_never_breaks_or_slows_a_request(tmp_path, backend):
    server = None
    if backend == "down":
        url = _closed_port_url()
    else:
        server = _Backend(status={"503": 503, "401 bad key": 401}.get(backend, 200),
                          delay=3.0 if backend.startswith("hangs") else 0.0)  # past the 1 s timeout
        url = server.url
    try:
        # baseline: the same 300 requests with no exporter at all
        base_t = tracing.Tracer(str(tmp_path / "base.sqlite"), exporter=False)
        start = time.perf_counter()
        for i in range(150):
            with base_t.trace("run", question=f"q{i}"):
                with base_t.span("plan", sql="SELECT 1"):
                    pass
                base_t.event("route", source="critic", destination="execute")
        baseline = time.perf_counter() - start

        took, t, exp = _run_requests(tmp_path, url, n=150)   # must not raise
        # the export happens off the request path: at most a small overhead
        assert took < baseline * 3 + 1.0, (backend, took, baseline)
        # local traces are written regardless of the backend
        assert len(t.list_traces(limit=1000)) == 150
        if server is not None:
            exp.flush(8000)
            assert server.requests, "the exporter never tried the backend"
    finally:
        exp_ = locals().get("exp")
        if exp_ is not None:
            exp_.shutdown()
        if server is not None:
            server.close()


def test_the_queue_is_bounded_when_the_backend_hangs(tmp_path):
    # more spans than the queue holds, behind a hanging backend: the extra ones
    # are dropped, nothing blocks, memory stays bounded (800 runs x 4 spans)
    server = _Backend(delay=3.0)
    try:
        took, t, exp = _run_requests(tmp_path, server.url, n=800)    # ~3,200 spans > the queue
        assert took < 15, took
        assert len(exp.processor._batch_processor._queue) <= trace_export.QUEUE_MAX
    finally:
        exp.shutdown()
        server.close()


def test_the_auth_header_is_sent_and_never_recorded_or_logged(tmp_path, monkeypatch, caplog):
    server = _Backend(status=401)
    monkeypatch.setenv(trace_export.ENDPOINT_ENV, server.url)
    monkeypatch.setenv(trace_export.HEADERS_ENV,
                       f"Authorization={SECRET},x-langfuse-ingestion-version=4")
    caplog.set_level(logging.DEBUG)
    try:
        exp = trace_export.from_env()
        t = tracing.Tracer(str(tmp_path / "t.sqlite"), exporter=exp)
        with t.trace("run", question="q") as tid:
            t.event("plan")
        exp.flush(8000)
        sent = {k.lower(): v for k, v in server.requests[0].items()} if server.requests else {}
        assert sent.get("authorization") == SECRET          # header names are case-insensitive
        assert sent.get("x-langfuse-ingestion-version") == "4"
        assert SECRET not in repr(t.get_trace(tid))                       # not recorded
        assert SECRET not in caplog.text                                  # not logged
        db = open(tmp_path / "t.sqlite", "rb").read()
        assert SECRET.encode() not in db
    finally:
        exp.shutdown()
        server.close()
        trace_export._cached.clear()


def test_header_parsing_keeps_base64_padding_and_skips_junk():
    h = trace_export.parse_headers("Authorization=Basic YWI6Y2Q=, x-langfuse-ingestion-version=4,"
                                   " junk, =novalue")
    assert h == {"Authorization": "Basic YWI6Y2Q=", "x-langfuse-ingestion-version": "4"}


# Free-text attributes the tracer records (in SQLite, and to a local Phoenix).
# Result rows never; an execution error is reduced to its class
# (tests/integration/test_indirect_injection.py). A HOSTED backend gets less
# (owner's decision 2026-11-07, test below): no question (a hash instead), SQL
# literals masked, no approval reason, reviewer or escalation identity. This
# list is pinned, so a new free-text attribute needs a privacy review first.
FREE_TEXT_EXPORTED = {"question", "sql", "reason", "issues", "why", "decided_by",
                      "escalated_to", "error"}
# withheld from a hosted backend, whatever span carries them
NEVER_HOSTED = {"question", "escalated_to"}


def test_every_free_text_attribute_is_a_reviewed_one():
    import glob
    import re
    root = os.path.join(os.path.dirname(__file__), "..", "..")
    keys = set()
    for f in glob.glob(os.path.join(root, "*.py")):
        src = open(f, encoding="utf-8").read()
        for m in re.finditer(r"tracer\.(?:span|event|trace|start_trace|end_trace)\(|\.set\(", src):
            i, depth = m.end(), 1
            while depth and i < len(src):
                depth += {"(": 1, ")": -1}.get(src[i], 0)
                i += 1
            keys |= set(re.findall(r"\b(\w+)=", src[m.start():i]))
    numeric_or_enum = {"attempt", "attempts", "retry_context", "routing", "sql_sha256", "backend",
                       "row_count", "outcome", "destination", "source", "trace_id", "critic",
                       "dataset", "max_attempts", "mode", "from_node", "status", "total_cost_usd",
                       "total_tokens", "model", "verdict", "allowed", "approval_id", "capability",
                       "by", "decision", "terminal", "committed", "est_cost_usd",
                       "provider", "prompt_tokens", "completion_tokens", "latency_ms"}
    unreviewed = keys - numeric_or_enum - FREE_TEXT_EXPORTED
    # keyword arguments that are not span attributes (python syntax in the calls)
    # (`read=`/`quoted=` are sqlglot arguments inside node .set(...) calls in phi_guard)
    unreviewed -= {"exporter", "name", "end", "this", "read", "quoted"}
    assert not unreviewed, f"new trace attributes need a privacy review: {sorted(unreviewed)}"



PERSON = "Jane Doe 02134"
HOSTED_URL = "https://us.cloud.langfuse.com/api/public/otel/v1/traces"


def _traced(tmp_path, endpoint, mem):
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    exp = trace_export.OTLPExporter(endpoint, processor=SimpleSpanProcessor(mem))
    t = tracing.Tracer(str(tmp_path / "t.sqlite"), exporter=exp)
    with t.trace("run", question=f"how many visits did {PERSON} have?") as tid:
        with t.span("plan", sql=f"SELECT count(*) FROM gold_encounter WHERE x = '{PERSON}'",
                    model="m"):
            pass
    with t.attach(tid):
        t.event("approval.decision", decision="escalate", decided_by="dr.reviewer@example.org",
                escalated_to="nurse.lead@example.org", reason=f"saw {PERSON} in the rows",
                terminal=False)
    return t, tid


def test_a_hosted_backend_gets_no_question_reviewer_text_or_identities(tmp_path):
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    mem = InMemorySpanExporter()
    t, tid = _traced(tmp_path, HOSTED_URL, mem)
    spans = {s.name: s for s in mem.get_finished_spans()}
    sent = repr([dict(s.attributes) for s in spans.values()])
    for withheld in (PERSON, "Jane Doe", "dr.reviewer@example.org", "nurse.lead@example.org"):
        assert withheld not in sent, withheld
    sent_keys = {k for s in spans.values() for k in s.attributes}
    for key in NEVER_HOSTED:
        assert f"agent.{key}" not in sent_keys, key
    assert "agent.reason" not in dict(spans["approval.decision"].attributes)
    assert "agent.decided_by" not in dict(spans["approval.decision"].attributes)
    assert spans["approval.decision"].attributes["agent.decision"] == "escalate"
    run = dict(spans["run"].attributes)
    assert len(run["agent.question_sha256"]) == 16 and run["agent.question_chars"] > 0
    assert "input.value" not in run
    assert spans["plan"].attributes["agent.sql"].endswith("x = '<value>'")
    # the local record keeps everything
    local = repr(t.get_trace(tid))
    assert PERSON in local and "dr.reviewer@example.org" in local


def test_a_local_phoenix_still_gets_the_full_record(tmp_path):
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    mem = InMemorySpanExporter()
    _traced(tmp_path, "http://localhost:6006/v1/traces", mem)
    run = {s.name: s for s in mem.get_finished_spans()}["run"]
    assert PERSON in run.attributes["input.value"]


@pytest.mark.parametrize("url, hosted", [
    ("http://localhost:6006/v1/traces", False), ("http://127.0.0.1:6006/v1/traces", False),
    (HOSTED_URL, True), ("https://cloud.langfuse.com/api/public/otel/v1/traces", True),
    ("http://phoenix.internal:6006/v1/traces", True)])
def test_anything_off_this_machine_counts_as_hosted(url, hosted):
    assert trace_export.is_hosted(url) is hosted
