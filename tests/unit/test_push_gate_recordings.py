"""The push gate's recordings match what the planner would send today (item 13).

The push gate replays committed answers (tests/evals/recordings/push_gate.json).
Each is keyed on the exact request: role, a hash of the system prompt (which
carries the schema notes), and the question. If a prompt, a schema note or a
subset question changes without re-recording, this test fails on every push --
before the replayed gate would fail with REPLAY MISS -- and says how to fix it:

    python scripts/record_push_gate.py
"""

import json
import os
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)

import eval_bank
import eval_bank_fhir
import replay
from nl_to_sql_planner import SCHEMA_DOC_FHIR, SCHEMA_DOC_HOSPITAL, build_system_prompt

STORE = os.path.join(ROOT, "tests", "evals", "recordings", "push_gate.json")
BANKS = {
    "hospital": (eval_bank, SCHEMA_DOC_HOSPITAL),
    "fhir": (eval_bank_fhir, SCHEMA_DOC_FHIR),
}
FIX = "re-record: python scripts/record_push_gate.py"


def _entries():
    with open(STORE, encoding="utf-8") as f:
        return json.load(f)


def _expected_keys(dataset):
    bank, schema = BANKS[dataset]
    system = build_system_prompt(schema)
    return {c["id"]: replay.request_key("plan", system, c["question"])
            for c in bank.CASES if c["id"] in bank.SUBSET_IDS}


@pytest.mark.parametrize("dataset", sorted(BANKS))
def test_every_subset_case_has_a_recording_for_todays_prompt(dataset):
    entries = _entries()
    missing = [cid for cid, key in _expected_keys(dataset).items() if key not in entries]
    assert not missing, f"{dataset} cases with no recording for the current prompt: {missing}; {FIX}"


def test_no_stale_recordings_are_left_behind():
    wanted = set().union(*(_expected_keys(d).values() for d in BANKS))
    stale = sorted(e.get("case_id") or k for k, e in _entries().items() if k not in wanted)
    assert not stale, f"recordings no current request asks for: {stale}; {FIX}"


def test_an_authored_answer_still_matches_its_case_reference_sql():
    # the key holds the question, not the reference: an edited reference_sql
    # would otherwise replay the old SQL and fail as a wrong answer, unexplained
    import hashlib
    refs = {c["id"]: c["reference_sql"] for b, _ in BANKS.values() for c in b.CASES}
    stale = [e["case_id"] for e in _entries().values() if e.get("authored")
             and hashlib.sha256(refs[e["case_id"]].encode()).hexdigest()[:16]
             != e.get("reference_sha256")]
    assert not stale, f"reference_sql changed since recording: {stale}; {FIX}"


def test_a_changed_prompt_is_a_miss_not_a_silent_pass():
    # the property the gate rests on: one character of schema note is a new key
    bank, schema = BANKS["hospital"]
    case = next(c for c in bank.CASES if c["id"] in bank.SUBSET_IDS)
    changed = build_system_prompt(schema + "\n- a new note")
    store = replay.ReplayStore(STORE)
    with pytest.raises(replay.ReplayMiss):
        store.lookup("plan", changed, case["question"])


def test_recordings_hold_sql_and_counts_only():
    # same rule as the UI E2E recordings: no keys, no result rows
    for e in _entries().values():
        assert set(e) <= {"role", "user", "text", "prompt_tokens", "completion_tokens",
                          "model", "authored", "dataset", "case_id", "reference_sha256"}, e
        assert e["role"] == "plan"


def test_the_harness_fails_the_gate_on_a_replay_miss():
    import eval_harness as eh
    assert eh._error_mode(replay.ReplayMiss("replay miss: no recording")) == eh.F_REPLAY_MISS
    assert eh._error_mode(RuntimeError("Error code: 429")) == eh.F_PROVIDER_ERROR
    assert eh._error_mode(ValueError("bad")) == eh.F_MODEL_ERROR


def test_a_missing_recording_turns_the_real_gate_red(tmp_path):
    # the hospital push gate, end to end on replay, with one recording removed:
    # 5 of 6 correct would clear the 80% bar, so only the miss rule can fail it
    import subprocess
    entries = _entries()
    hospital = [k for k, e in entries.items() if e.get("dataset") == "hospital"]
    del entries[hospital[0]]
    store = tmp_path / "push_gate.json"
    store.write_text(json.dumps(entries))
    env = dict(os.environ, PLANNER_PROVIDER="replay", ALLOW_REPLAY="1",
               REPLAY_STORE=str(store), EVAL_SUBSET="1", EVAL_RUNS="1",
               EVAL_DATASET="hospital", EVAL_OUT_DIR=str(tmp_path))
    out = subprocess.run([sys.executable, "eval_harness.py"], cwd=ROOT, env=env,
                         capture_output=True, text=True, timeout=300)
    assert out.returncode == 1, out.stdout[-2000:] + out.stderr[-2000:]
    assert "REPLAY MISS" in out.stdout and "replay_miss: 1" in out.stdout
