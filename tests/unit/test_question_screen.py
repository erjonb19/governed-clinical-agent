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

    saved = dict(tool_base._TOOL_REGISTRY)
    tool_base._TOOL_REGISTRY.pop("analytics.query_aggregate", None)
    request_cleanup = lambda: (
        tool_base._TOOL_REGISTRY.clear(),
        tool_base._TOOL_REGISTRY.update(saved),
    )  # noqa: E731
    import app as app_module

    try:
        importlib.reload(app_module)
    except Exception:
        request_cleanup()
        raise
    import nl_to_sql_planner

    def forbidden(*a, **k):
        raise AssertionError("a model was called")

    for name in ("generate_sql", "generate_sql_with_metrics", "critique"):
        monkeypatch.setattr(nl_to_sql_planner.NLToSQLPlanner, name, forbidden, raising=True)
    with TestClient(app_module.app) as c, caplog.at_level("WARNING", logger="governed.privacy"):
        for path in ("/query", "/propose"):
            r = c.post(path, headers={"X-API-Key": "k"}, json={"question": PLANTED["phone number"]})
            assert r.status_code == 422, (path, r.text)
            assert "phone number" in r.json()["detail"] and "555-0134" not in r.text
    assert "555-0134" not in caplog.text and "kinds=phone number" in caplog.text
    request_cleanup()
