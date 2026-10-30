"""critic.py: verdict parsing, fail-open on any error, and the two switches."""

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import critic
from nl_to_sql_planner import build_critic_prompt


def test_well_formed_verdicts():
    v = critic.parse_verdict('{"verdict": "approve", "reason": "fine", "issues": []}')
    assert v == {"verdict": "approve", "reason": "fine", "issues": []}
    v = critic.parse_verdict('{"verdict": "REVISE", "reason": "wrong column", "issues": ["x"]}')
    assert v["verdict"] == "revise" and v["issues"] == ["x"]


def test_fences_and_prose_around_the_json_are_tolerated():
    text = 'Here you go:\n```json\n{"verdict": "revise", "reason": "no LIMIT 5"}\n```'
    assert critic.parse_verdict(text)["verdict"] == "revise"


def test_anything_else_is_unavailable():
    for text in (None, "", "approve", "{not json}", '["approve"]',
                 '{"verdict": "maybe"}', '{"verdict": "allow"}', '{"reason": "no verdict"}'):
        assert critic.parse_verdict(text)["verdict"] == critic.UNAVAILABLE, text


def test_issues_that_are_not_a_list_are_wrapped():
    v = critic.parse_verdict('{"verdict": "revise", "reason": "r", "issues": "one thing"}')
    assert v["issues"] == ["one thing"]


class _Planner:
    def __init__(self, result):
        self.result = result

    def critique(self, question, sql, schema_doc=None):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


METRICS = SimpleNamespace(as_dict=lambda: {"total_tokens": 50, "est_cost_usd": 0.0001})


def test_review_returns_the_verdict_and_the_call_metrics():
    v = critic.review(_Planner(('{"verdict": "approve", "reason": "ok"}', METRICS)), "q", "SELECT 1")
    assert v["verdict"] == "approve" and v["metrics"]["total_tokens"] == 50


def test_review_fails_open_on_a_provider_error():
    # D4: an error is "unavailable", which goes on to the guard -- never a block
    v = critic.review(_Planner(RuntimeError("Error code: 429")), "q", "SELECT 1")
    assert v["verdict"] == critic.UNAVAILABLE and "429" in v["reason"] and v["metrics"] is None


def test_review_fails_open_for_a_planner_without_a_critic():
    v = critic.review(object(), "q", "SELECT 1")
    assert v["verdict"] == critic.UNAVAILABLE


def test_switches(monkeypatch):
    monkeypatch.delenv("CRITIC_ENABLED", raising=False)
    monkeypatch.delenv("CRITIC_ROUTING", raising=False)
    assert critic.enabled() and critic.routing()
    for off in ("0", "false", "OFF", "no"):
        monkeypatch.setenv("CRITIC_ENABLED", off)
        monkeypatch.setenv("CRITIC_ROUTING", off)
        assert not critic.enabled() and not critic.routing()


def test_the_critic_prompt_tells_it_to_ignore_instructions_in_the_question():
    p = build_critic_prompt("SCHEMA")
    assert "SCHEMA" in p and "untrusted" in p and "pre-approved" in p
    assert "do not decide what is allowed to run" in p.lower()


def test_the_eval_harness_scores_a_critic_exhausted_run_as_its_own_mode():
    # so a critic that blocks correct SQL shows up as "critic_rejected", not as
    # a guard denial or an SQL error
    import eval_harness as eh

    class Agent:
        def run(self, q):
            return {"allowed": False, "decided_by": "exhausted", "attempts": 3,
                    "reason": "gave up after 3 attempts: critic: wrong column"}

    mode, detail, attempts = eh.run_once_graph({"question": "q"}, Agent())
    assert mode == eh.F_CRITIC_REJECTED and attempts == 3
