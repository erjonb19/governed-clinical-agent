"""Trace export to a self-hosted OTLP backend such as Arize Phoenix (backlog item 2).

The exporter is fed through OpenTelemetry's in-memory exporter, so nothing
leaves the process: the tests check what WOULD be sent -- one trace per run,
the step hierarchy, the OpenInference span kinds Phoenix reads, error status,
and that result rows never appear.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

pytest.importorskip("opentelemetry.sdk")
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402

import trace_export  # noqa: E402
import tracing  # noqa: E402


@pytest.fixture
def mem():
    return InMemorySpanExporter()


@pytest.fixture
def tracer(tmp_path, mem):
    exp = trace_export.OTLPExporter("http://localhost/unused", processor=SimpleSpanProcessor(mem))
    return tracing.Tracer(str(tmp_path / "t.sqlite"), exporter=exp)


def _by_name(mem):
    return {s.name: s for s in mem.get_finished_spans()}


def test_a_run_becomes_one_trace_with_its_steps_under_a_root(tracer, mem):
    with tracer.trace("run", question="how many hospitals?") as tid:
        with tracer.span("plan", sql="SELECT 1", model="m", provider="gemini"):
            pass
        with tracer.span("guard", allowed=True):
            tracer.event("execute", row_count=1)
        tracer.event("route", source="execute", destination="finish")
    spans = _by_name(mem)
    assert set(spans) == {"run", "plan", "guard", "execute", "route"}
    trace_ids = {s.context.trace_id for s in spans.values()}
    assert len(trace_ids) == 1 and trace_ids == {int(tid, 16)}
    root = spans["run"]
    assert root.parent is None
    for top in ("plan", "guard", "route"):
        assert spans[top].parent.span_id == root.context.span_id, top
    assert spans["execute"].parent.span_id == spans["guard"].context.span_id
    assert root.start_time <= spans["plan"].start_time <= spans["plan"].end_time <= root.end_time


def test_spans_carry_the_openinference_fields_phoenix_reads(tracer, mem):
    with tracer.trace("run", question="q?"):
        with tracer.span("plan", question="q?", sql="SELECT 2", model="gemini-x",
                         provider="gemini"):
            pass
        with tracer.span("guard", allowed=False, reason="table not on Gold allowlist"):
            pass
    s = _by_name(mem)
    assert s["plan"].attributes["openinference.span.kind"] == "LLM"
    assert s["plan"].attributes["input.value"] == "q?"
    assert s["plan"].attributes["output.value"] == "SELECT 2"
    assert s["plan"].attributes["llm.model_name"] == "gemini-x"
    assert s["guard"].attributes["openinference.span.kind"] == "GUARDRAIL"
    assert s["guard"].attributes["agent.allowed"] is False
    assert s["run"].resource.attributes["openinference.project.name"] == "governed-clinical-agent"


def test_result_rows_are_never_exported(tracer, mem):
    secret = "Patient-Jane-Doe-02134"
    with tracer.trace("run", rows=[{"name": secret}]):
        with tracer.span("execute", rows=[{"name": secret}], result={"x": secret}, row_count=1):
            pass
    for s in mem.get_finished_spans():
        assert secret not in repr(dict(s.attributes)), s.name
    assert _by_name(mem)["execute"].attributes["agent.row_count"] == 1


def test_an_error_is_exported_as_an_error(tracer, mem):
    with pytest.raises(RuntimeError):
        with tracer.trace("run"):
            with tracer.span("plan"):
                raise RuntimeError("provider down")
    s = _by_name(mem)
    from opentelemetry.trace.status import StatusCode
    assert s["plan"].status.status_code == StatusCode.ERROR
    assert s["run"].status.status_code == StatusCode.ERROR


def test_a_step_appended_later_lands_under_the_same_trace(tracer, mem):
    # an approval decided hours later attaches to the run's trace
    with tracer.trace("run") as tid:
        tracer.event("approval.propose")
    with tracer.attach(tid):
        tracer.event("approval.decision", decision="approve")
    s = _by_name(mem)
    assert s["approval.decision"].context.trace_id == s["run"].context.trace_id
    assert s["approval.decision"].parent.span_id == s["run"].context.span_id


def test_a_failing_exporter_never_breaks_the_run(tmp_path):
    class Broken:
        def on_end(self, span):
            raise OSError("collector unreachable")

    exp = trace_export.OTLPExporter("http://localhost/unused", processor=Broken())
    t = tracing.Tracer(str(tmp_path / "t.sqlite"), exporter=exp)
    with t.trace("run") as tid:
        with t.span("plan"):
            pass
    assert [s["name"] for s in t.get_trace(tid)["spans"]] == ["plan"]   # SQLite still written


def test_export_is_off_unless_configured(monkeypatch):
    monkeypatch.delenv("TRACE_OTLP_ENDPOINT", raising=False)
    assert trace_export.from_env() is None


def test_a_non_hex_trace_id_still_maps_to_a_valid_otel_id():
    assert 0 < trace_export._int_id("approval-thread-7", 128) < (1 << 128)
    assert trace_export.root_span_id("abc") == trace_export.root_span_id("abc") != 0


def test_the_deploy_never_exports_or_runs_phoenix():
    # Owner's requirements (2026-11-06): Phoenix is local only, and Render keeps
    # the SQLite traces. Export from Render is opt-in: the owner sets
    # TRACE_OTLP_ENDPOINT / TRACE_OTLP_HEADERS in the Render dashboard (backlog
    # item 12, a hosted backend), never in render.yaml; nothing installs Phoenix,
    # and a tracer built without the variable has no exporter.
    root = os.path.join(os.path.dirname(__file__), "..", "..")
    render = open(os.path.join(root, "render.yaml"), encoding="utf-8").read()
    assert "TRACE_OTLP_ENDPOINT" not in render and "phoenix" not in render.lower()
    for req in ("requirements-api.txt", "requirements.txt"):
        assert "arize-phoenix" not in open(os.path.join(root, req), encoding="utf-8").read(), req


def test_a_tracer_without_the_variable_writes_sqlite_only(tmp_path, monkeypatch):
    monkeypatch.delenv("TRACE_OTLP_ENDPOINT", raising=False)
    t = tracing.Tracer(str(tmp_path / "t.sqlite"))
    assert t.exporter is None
    with t.trace("run") as tid:
        t.event("plan")
    assert [s["name"] for s in t.get_trace(tid)["spans"]] == ["plan"]


# --- review fixes (2026-11-06) --------------------------------------------------

def test_a_pending_approval_sends_its_root_only_at_the_decision(tracer, mem):
    # Phoenix keeps the FIRST span it gets for an id, so a root sent while
    # pending would hide the decision (found in review, confirmed on Phoenix)
    with tracer.preserved():          # start_trace sets the current trace; restore it
        tid = tracer.start_trace("approval", question="q")
        tracer.event("approval.propose")
        tracer.end_trace(tid, status="pending_approval")
        assert "approval" not in _by_name(mem)
        with tracer.attach(tid):
            tracer.event("approval.decision", decision="approve")
        tracer.end_trace(tid, status="ok", decision="approve")
    roots = [s for s in mem.get_finished_spans() if s.name == "approval"]
    assert len(roots) == 1 and roots[0].attributes["agent.decision"] == "approve"


def test_a_trace_ended_twice_sends_one_root_and_an_end_span(tracer, mem):
    # a failed run resumed later ends its trace again
    with tracer.preserved():
        tid = tracer.start_trace("run")
        tracer.end_trace(tid, status="error", error="RuntimeError: boom")
        tracer.end_trace(tid, status="ok", decided_by="executed")
    spans = mem.get_finished_spans()
    roots = [s for s in spans if s.name == "run"]
    ends = [s for s in spans if s.name == "run.end"]
    assert len(roots) == 1 and len(ends) == 1
    assert len({s.context.span_id for s in spans}) == len(spans)        # no duplicate ids
    assert ends[0].parent.span_id == roots[0].context.span_id
    assert ends[0].attributes["agent.decided_by"] == "executed"


def test_error_text_is_exported_as_its_type_only(tracer, mem):
    value = "Could not convert string 'Jane Doe 02134' to INT32"
    with pytest.raises(ValueError):
        with tracer.trace("run"):
            with tracer.span("execute"):
                raise ValueError(value)
    for s in mem.get_finished_spans():
        assert "Jane Doe" not in repr(dict(s.attributes)) and "Jane Doe" not in str(s.status.description)
    assert _by_name(mem)["execute"].attributes["agent.error"] == "ValueError"


def test_exporter_false_forces_export_off(tmp_path, monkeypatch):
    monkeypatch.setenv("TRACE_OTLP_ENDPOINT", "http://localhost:1/v1/traces")
    assert tracing.Tracer(str(tmp_path / "t.sqlite"), exporter=False).exporter is None


def test_error_type_keeps_only_an_exception_name():
    assert trace_export.error_type("ValueError: bad 'Jane'") == "ValueError"
    assert trace_export.error_type("duckdb.ConversionException: x") == "duckdb.ConversionException"
    assert trace_export.error_type("patient Jane Doe: not found") == "error"
