"""The replay provider (PLAN v2 7a): recorded responses, a loud miss, no network."""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import replay
from nl_to_sql_planner import NLToSQLPlanner


def test_the_key_covers_role_schema_and_user_text():
    k = replay.request_key("plan", "SCHEMA A", "q")
    assert k == replay.request_key("plan", "SCHEMA A", "q")
    assert k != replay.request_key("critic", "SCHEMA A", "q")
    assert k != replay.request_key("plan", "SCHEMA B", "q")
    assert k != replay.request_key("plan", "SCHEMA A", "q\n\nYour previous SQL attempt was REJECTED")


def test_a_miss_fails_loudly(tmp_path):
    store = replay.ReplayStore(str(tmp_path / "llm.json"))
    with pytest.raises(replay.ReplayMiss, match="Re-record"):
        store.lookup("plan", "S", "how many?")


def test_record_mode_answers_a_miss_from_the_source_and_saves_it(tmp_path):
    path = tmp_path / "llm.json"
    calls = []

    def source():
        calls.append(1)
        return "SELECT 1", 100, 5, "real-model"

    rec = replay.ReplayStore(str(path), record=True)
    assert rec.lookup("plan", "S", "q", source) == ("SELECT 1", 100, 5, "real-model")
    assert rec.lookup("plan", "S", "q", source) == ("SELECT 1", 100, 5, "real-model")
    assert calls == [1]                                   # the second was a hit
    saved = json.loads(path.read_text())
    (entry,) = saved.values()
    assert entry["text"] == "SELECT 1" and entry["role"] == "plan"
    # replaying from the file needs no source at all
    assert replay.ReplayStore(str(path)).lookup("plan", "S", "q") == ("SELECT 1", 100, 5, "real-model")


def test_without_record_mode_a_source_is_never_called(tmp_path):
    store = replay.ReplayStore(str(tmp_path / "llm.json"), record=False)
    with pytest.raises(replay.ReplayMiss):
        store.lookup("plan", "S", "q", source=lambda: pytest.fail("network fallback"))


def test_the_planner_replays_plan_and_critic_calls(tmp_path, monkeypatch):
    from nl_to_sql_planner import SYSTEM_PROMPT, build_critic_prompt, SCHEMA_DOC
    path = tmp_path / "llm.json"
    entries = {
        replay.request_key("plan", SYSTEM_PROMPT, "how many?"):
            {"role": "plan", "user": "how many?", "text": "```sql\nSELECT 1;\n```",
             "prompt_tokens": 900, "completion_tokens": 12, "model": "recorded"},
        replay.request_key("critic", build_critic_prompt(SCHEMA_DOC),
                           "Question:\nhow many?\n\nProposed SQL:\nSELECT 1"):
            {"role": "critic", "user": "", "text": '{"verdict": "approve", "reason": "ok"}',
             "prompt_tokens": 950, "completion_tokens": 20, "model": "recorded"},
    }
    path.write_text(json.dumps(entries))
    monkeypatch.setenv("REPLAY_STORE", str(path))
    monkeypatch.delenv("E2E_RECORD", raising=False)
    p = NLToSQLPlanner(provider="replay")          # no key needed
    sql, m = p.generate_sql_with_metrics("how many?")
    assert sql == "SELECT 1" and m.provider == "replay" and m.prompt_tokens == 900
    text, m2 = p.critique("how many?", "SELECT 1")
    assert json.loads(text)["verdict"] == "approve" and m2.completion_tokens == 20
    with pytest.raises(replay.ReplayMiss):
        p.generate_sql_with_metrics("something never recorded")


def test_no_recording_holds_a_key_or_rows():
    path = replay.DEFAULT_STORE
    if not os.path.exists(path):
        pytest.skip("no recordings yet")
    text = open(path, encoding="utf-8").read()
    for marker in ("sk-ant-", "AIza", "gsk_", "xai-", '"rows"'):
        assert marker not in text
