"""Backlog item 52: the standalone MCP server (stdio or HTTP) writes an audit
log of its own, records each tool call, and never writes result rows."""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
pytest.importorskip("mcp", reason="the MCP front door needs the mcp package")

import auth_failures  # noqa: E402
import mcp_server as M  # noqa: E402


@pytest.fixture
def fresh(monkeypatch):
    import src.tools.base as tool_base

    saved_reg = dict(tool_base._TOOL_REGISTRY)
    for tool in (M.EchoTool(), M._analytics):
        tool_base._TOOL_REGISTRY.pop(tool.name, None)
        M.runtime.register_tool(tool)
    monkeypatch.setattr(M.runtime, "audit_logger", None)
    monkeypatch.setattr(auth_failures.FAILURES, "audit", None)
    try:
        yield
    finally:
        tool_base._TOOL_REGISTRY.clear()
        tool_base._TOOL_REGISTRY.update(saved_reg)


def _events(d):
    return [
        json.loads(x)
        for f in d.glob("audit_mcp_*.jsonl")
        for x in f.read_text().splitlines()
        if x.strip()
    ]


def test_main_attaches_an_audit_log(fresh, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AUDIT_LOG_DIR", str(tmp_path))
    monkeypatch.setattr(M.mcp, "run", lambda *a, **k: None)  # stdio: do not block
    M.main(["--transport", "stdio"])
    assert M.runtime.audit_logger is not None
    assert auth_failures.FAILURES.audit is M.runtime.audit_logger
    assert capsys.readouterr().out == ""  # stdout is stdio's protocol channel


def test_an_unwritable_log_dir_stops_the_server_with_a_message(fresh, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with pytest.raises(SystemExit, match="AUDIT_LOG_DIR"):
        M.attach_standalone_audit(str(blocker / "logs"))


def test_tool_calls_are_audited_without_rows(fresh, tmp_path):
    M.attach_standalone_audit(str(tmp_path))
    if not M.available_datasets():
        out = M.query_analytics("SELECT * FROM bronze_patient", "fhir")  # a refusal
        assert out.startswith("REFUSED")
    else:
        ds = M.available_datasets()[0]
        table = "gold_hospital_profile" if ds == "hospital" else "gold_patient"
        M.query_analytics(f"SELECT count(*) AS n FROM {table}", ds)
    ev = _events(tmp_path)
    assert ev, "no audit event written"
    assert all(e["agent_id"] == "mcp" for e in ev)
    assert not any('"rows"' in json.dumps(e.get("execution_result") or {}) for e in ev)


def test_the_logger_drops_rows():
    from src.runtime.audit_logger import RowlessAuditLogger

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        log = RowlessAuditLogger(
            log_dir=Path(d), agent_id="mcp", max_buffer_size=1, enable_console=False
        )
        log.log_tool_execution(
            "analytics.query_aggregate",
            {"sql": "SELECT 1"},
            True,
            1.0,
            result={"output": {"rows": [{"zip": "02134"}], "row_count": 1}},
        )
        text = "".join(p.read_text() for p in Path(d).glob("*.jsonl"))
    assert "02134" not in text and '"row_count": 1' in text


def test_a_host_s_logger_is_kept(fresh, tmp_path):
    sentinel = object()
    M.runtime.audit_logger = sentinel
    assert M.attach_standalone_audit(str(tmp_path)) is sentinel


def test_empty_dir_turns_it_off_with_a_warning(fresh, capsys):
    assert M.attach_standalone_audit("") is None
    err = capsys.readouterr()
    assert "not audited" in err.err and err.out == ""
