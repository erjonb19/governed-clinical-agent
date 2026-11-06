"""scripts/eval_gate.sh: the eval harness's exit code becomes the build's verdict.

Backlog item 1 (owner's decisions 2026-11-06): a regression is always red; a
provider outage only warns on push, but nightly it is retried once and then
turns red. The harness is stubbed (EVAL_CMD) with a script that replays exit
codes from a file, so no model is called.
"""

import os
import shutil
import subprocess

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
GATE = os.path.join(ROOT, "scripts", "eval_gate.sh")

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def gate(tmp_path, mode, codes, extra_env=None, sleep=None):
    """Run the gate with a stub harness that exits with codes[0], codes[1], ...
    Returns (exit code, output, number of harness runs)."""
    seq = tmp_path / "codes"
    seq.write_text("\n".join(str(c) for c in codes) + "\n")
    count = tmp_path / "count"
    count.write_text("0")
    stub = tmp_path / "harness.sh"
    stub.write_text(f"""#!/usr/bin/env bash
n=$(cat {count}); n=$((n+1)); echo $n > {count}
{"sleep " + str(sleep) if sleep else ""}
exit $(sed -n "${{n}}p" {seq})
""")
    stub.chmod(0o755)
    env = {**os.environ, "EVAL_CMD": str(stub), "EVAL_RETRY_WAIT_SEC": "0", **(extra_env or {})}
    p = subprocess.run(["bash", GATE, mode, "--runs", "3"], env=env, capture_output=True,
                       text=True)
    return p.returncode, p.stdout + p.stderr, int(count.read_text())


@pytest.mark.parametrize("mode", ["warn", "retry-then-fail"])
def test_a_pass_is_green_and_runs_once(tmp_path, mode):
    assert gate(tmp_path, mode, [0])[::2] == (0, 1)


@pytest.mark.parametrize("mode", ["warn", "retry-then-fail"])
def test_a_regression_is_red_and_never_retried(tmp_path, mode):
    code, out, runs = gate(tmp_path, mode, [1, 0])
    assert (code, runs) == (1, 1) and "Eval below threshold" in out


def test_on_push_a_provider_outage_warns_and_is_not_retried(tmp_path):
    code, out, runs = gate(tmp_path, "warn", [2, 0])
    assert (code, runs) == (0, 1) and "::warning title=Provider unavailable" in out


def test_nightly_a_provider_outage_is_retried_once_then_red(tmp_path):
    code, out, runs = gate(tmp_path, "retry-then-fail", [2, 2, 0])
    assert (code, runs) == (2, 2)
    assert "::error title=Provider unavailable" in out


def test_nightly_a_retry_that_passes_is_green(tmp_path):
    assert gate(tmp_path, "retry-then-fail", [2, 0])[::2] == (0, 2)


def test_nightly_a_retry_that_regresses_is_red(tmp_path):
    code, out, runs = gate(tmp_path, "retry-then-fail", [2, 1])
    assert (code, runs) == (1, 2) and "Eval below threshold" in out


def test_an_unknown_mode_is_refused(tmp_path):
    assert gate(tmp_path, "maybe", [0])[0] == 64


def test_an_attempt_past_its_time_limit_is_inconclusive_retried_then_red(tmp_path):
    # 0.02 min = 1.2 s; the stub sleeps 5 s on every attempt
    code, out, runs = gate(tmp_path, "retry-then-fail", [0, 0],
                           extra_env={"EVAL_ATTEMPT_TIMEOUT_MIN": "0.02"}, sleep=5)
    assert (code, runs) == (2, 2)
    assert "Eval attempt timed out" in out and "::error title=Provider unavailable" in out
