"""Audit item 41: a question carrying a personal identifier is refused before any
model call, and only the kinds found are recorded."""

import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import question_screen  # noqa: E402

PLANTED = {
    "email address": "How many encounters does jane.doe@example.org have?",
    "social security number": "Find the patient with SSN 123-45-6789",
    "phone number": "Who called from (718) 555-0134?",
    "record number": "Show conditions for MRN 4471923",
    "patient identifier": "Count encounters for patient 1d9a2c4e-3b7f-4d2e-9a1b-0c2d3e4f5a6b",
    "full date": "Who was born on 03/14/1961?",
}


@pytest.mark.parametrize("kind", list(PLANTED))
def test_each_planted_identifier_is_found(kind):
    assert kind in question_screen.identifiers_in(PLANTED[kind])


def test_no_eval_question_is_refused():
    # every question in every eval bank must still reach the model
    n = 0
    for mod in ("eval_bank", "eval_bank_fhir", "eval_bank_adversarial"):
        bank = importlib.import_module(mod)
        for value in vars(bank).values():
            if (
                isinstance(value, list)
                and value
                and isinstance(value[0], dict)
                and "question" in value[0]
            ):
                for case in value:
                    n += 1
                    assert question_screen.identifiers_in(case["question"]) == [], case.get("id")
    assert n >= 80


@pytest.mark.parametrize(
    "q",
    [
        "How many patients are aged 90 or older?",
        "Hospitals in zip 10461 with 5 stars",
        "Encounters between 2019 and 2020",
        "Top 10 hospitals by star rating",
        "Average glucose in mg/dL above 100",
    ],
)
def test_ordinary_questions_pass(q):
    assert question_screen.identifiers_in(q) == []


def test_the_refusal_names_kinds_not_values():
    msg = question_screen.refusal(question_screen.identifiers_in(PLANTED["social security number"]))
    assert "social security number" in msg and "123-45-6789" not in msg


def test_query_and_propose_refuse_before_any_model_call(tmp_path, monkeypatch, caplog):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    for k, v in {
        "API_KEY": "k",
        "TRACE_DB": tmp_path / "t.sqlite",
        "CHECKPOINT_DB": tmp_path / "c.sqlite",
        "APPROVALS_DB": tmp_path / "a.sqlite",
        "AUDIT_LOG_DIR": tmp_path / "audit",
    }.items():
        monkeypatch.setenv(k, str(v))
    import src.tools.base as tool_base
    import tracing

    saved = dict(tool_base._TOOL_REGISTRY)
    tool_base._TOOL_REGISTRY.pop("analytics.query_aggregate", None)
    tracing.set_tracer(None)
    importlib.reload(tracing)
    try:
        import app as app_module

        importlib.reload(app_module)
        reserved = []
        monkeypatch.setattr(
            app_module.llm_budget.BUDGET, "reserve", lambda *a, **k: reserved.append(1)
        )
        with TestClient(app_module.app) as c, caplog.at_level("WARNING", logger="governed.privacy"):
            for path in ("/query", "/propose", "/v1/query"):
                r = c.post(
                    path, headers={"X-API-Key": "k"}, json={"question": PLANTED["phone number"]}
                )
                assert r.status_code == 422, (path, r.text)
                assert "phone number" in r.json()["detail"] and "555-0134" not in r.text
            traces = c.get("/traces", headers={"X-API-Key": "k"}).json()["traces"]
        assert reserved == []  # refused before the model budget, so before any model
        assert "555-0134" not in caplog.text and "kinds=phone number" in caplog.text
        screened = [t for t in traces if t["name"] == "question_screen"]
        assert len(screened) == 3 and all(
            t["attrs"].get("kinds") == "phone number" for t in screened
        )
        assert "555-0134" not in str(traces)
    finally:
        tracing.set_tracer(None)
        tool_base._TOOL_REGISTRY.clear()
        tool_base._TOOL_REGISTRY.update(saved)


@pytest.mark.parametrize(
    "q",
    [
        "Call (718)555-0134",
        "+1 718 555 0134",
        "SSN 123456789",
        "born 14 Mar 1961",
        "born March 14th, 1961",
        "seen 14.03.1961",
    ],
)
def test_more_formats_are_found(q):
    assert question_screen.identifiers_in(q), q
