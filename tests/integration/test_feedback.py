"""Backlog item 24: thumbs up or down on an answer, and thumbs-down to a candidate case.

A rating is stored with the answer's trace id (the trace must exist), a later
rating replaces the first, the free-text reason is screened like a question, the
keyed /metrics shows the share rated down, and scripts/feedback_to_cases.py turns
each thumbs-down into a candidate eval case file with its trace link.
"""

import json
import os
import sys

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
H = {"X-API-Key": "test-key"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    for k, v in {
        "API_KEY": "test-key",
        "TRACE_DB": tmp_path / "t.sqlite",
        "CHECKPOINT_DB": tmp_path / "c.sqlite",
        "APPROVALS_DB": tmp_path / "a.sqlite",
        "AUDIT_LOG_DIR": tmp_path / "audit",
        "PLANNER_METRICS_LOG": tmp_path / "m.jsonl",
    }.items():
        monkeypatch.setenv(k, str(v))
    import importlib

    import feedback
    import src.tools.base as tool_base
    import tracing

    monkeypatch.setattr(feedback, "FEEDBACK_DB", str(tmp_path / "f.sqlite"))
    saved = dict(tool_base._TOOL_REGISTRY)
    tool_base._TOOL_REGISTRY.pop("analytics.query_aggregate", None)
    tracing.set_tracer(tracing.Tracer(str(tmp_path / "t.sqlite")))
    import app as m

    importlib.reload(m)
    try:
        with TestClient(m.app) as c:
            yield c
    finally:
        tracing.set_tracer(None)
        tool_base._TOOL_REGISTRY.clear()
        tool_base._TOOL_REGISTRY.update(saved)


def _answer(c):
    r = c.post("/raw-sql", headers=H, json={"sql": "SELECT 1 AS n"})
    assert r.status_code == 200
    return r.json()["trace_id"]


def test_a_rating_is_stored_and_a_later_one_replaces_it(client):
    tid = _answer(client)
    assert (
        client.post("/feedback", headers=H, json={"trace_id": tid, "rating": "up"}).status_code
        == 200
    )
    r = client.post(
        "/feedback",
        headers=H,
        json={"trace_id": tid, "rating": "down", "reason": "the count was wrong"},
    )
    assert r.status_code == 200 and r.json()["rating"] == "down"
    fb = client.get("/metrics", headers=H).json()["feedback"]
    assert fb == {"total": 1, "up": 0, "down": 1, "down_share": 1.0}


def test_metrics_show_the_share_rated_down(client):
    a, b = _answer(client), _answer(client)
    client.post("/feedback", headers=H, json={"trace_id": a, "rating": "up"})
    client.post("/feedback", headers=H, json={"trace_id": b, "rating": "down"})
    fb = client.get("/metrics", headers=H).json()["feedback"]
    assert fb["down_share"] == 0.5 and "reason" not in json.dumps(fb)


@pytest.mark.parametrize(
    "body, code",
    [
        ({"trace_id": "no-such-trace-0001", "rating": "up"}, 404),
        ({"trace_id": "short", "rating": "up"}, 422),
        ({"trace_id": "x" * 20, "rating": "meh"}, 422),
        ({"trace_id": "x" * 20, "rating": "up", "extra": 1}, 422),
        ({"trace_id": "../../etc/passwd", "rating": "up"}, 422),
    ],
)
def test_bad_ratings_are_refused(client, body, code):
    assert client.post("/feedback", headers=H, json=body).status_code == code


def test_a_reason_with_an_identifier_is_refused_and_not_stored(client):
    tid = _answer(client)
    r = client.post(
        "/feedback",
        headers=H,
        json={"trace_id": tid, "rating": "down", "reason": "call me at 617-555-0142"},
    )
    assert r.status_code == 422 and "617-555-0142" not in r.text
    assert client.get("/metrics", headers=H).json()["feedback"]["total"] == 0


def test_rating_needs_the_key(client):
    tid = _answer(client)
    assert client.post("/feedback", json={"trace_id": tid, "rating": "up"}).status_code == 401


def test_a_thumbs_down_becomes_a_candidate_case(client, tmp_path):
    tid = _answer(client)
    client.post(
        "/feedback", headers=H, json={"trace_id": tid, "rating": "down", "reason": "wrong table"}
    )
    import feedback_to_cases

    out = tmp_path / "cands"
    assert feedback_to_cases.main(["--out", str(out), "--base-url", "https://example.org"]) == 0
    files = list(out.iterdir())
    assert len(files) == 1
    case = json.loads(files[0].read_text())
    assert case["trace"] == f"https://example.org/traces/{tid}" and case["trace_found"]
    assert case["agent_sql"] and case["rater_reason"] == "wrong table"
    assert case["reference_sql"] == "TODO"
    # running again writes nothing new
    assert feedback_to_cases.main(["--out", str(out)]) == 0 and len(list(out.iterdir())) == 1
