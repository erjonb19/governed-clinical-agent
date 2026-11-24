"""Backlog item 68: the adversarial gate runs on every push, with no model.

scripts/adversarial_gate.py plays a worst-case model (always the attack, a critic
that always approves) through the real harness, graph and guard. These tests pin
that it passes with the real guard, FAILS with a guard that allows everything
(so it cannot pass vacuously), and that every case in the bank has an attack the
guard refuses or suppresses. The FHIR side uses the synthetic FHIR Gold
(tests/security/fhir_fixture.py); CI's push gate runs it on the real one.
"""

import os
import subprocess
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "tests", "security"))

HOSPITAL_GOLD = os.path.join(ROOT, "medallion", "hospital_gold.duckdb")
pytestmark = pytest.mark.skipif(not os.path.exists(HOSPITAL_GOLD), reason="no hospital Gold")

SABOTAGE = """
import os, sys
sys.path.insert(0, {root!r}); sys.path.insert(0, os.path.join({root!r}, "scripts"))
import sql_guard
# a guard that lets every query through unchanged (review of #180): the database
# then stops most attacks itself, which the strict check must not count as a pass
sql_guard.validate_query = lambda sql, *a, **k: sql_guard.Decision(True, "ok", safe_sql=sql)
import adversarial_gate
sys.exit(adversarial_gate.main())
"""


def _run(tmp_path, code=None):
    import fhir_fixture

    fhir = fhir_fixture.build(str(tmp_path / "fhir_gold.duckdb"))
    env = {
        **os.environ,
        "FHIR_GOLD_DB": fhir,
        "TRACE_DB": str(tmp_path / "t.sqlite"),
        "CHECKPOINT_DB": str(tmp_path / "c.sqlite"),
    }
    env.pop("STATE_DB_URL", None)
    cmd = (
        [sys.executable, "-c", code]
        if code
        else [sys.executable, os.path.join(ROOT, "scripts", "adversarial_gate.py")]
    )
    return subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, timeout=600)


def test_the_gate_passes_with_the_real_guard(tmp_path):
    r = _run(tmp_path)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-2000:]
    assert (
        "adversarial gate (100% of completed runs pass guard + stopping + tables): PASS" in r.stdout
    )


def test_the_gate_fails_when_the_guard_lets_everything_through(tmp_path):
    import adversarial_gate
    import eval_bank_adversarial as bank

    r = _run(tmp_path, SABOTAGE.format(root=ROOT))
    assert r.returncode == 1, r.stdout[-3000:]
    assert "adversarial gate, strict: FAIL" in r.stdout, r.stdout[-3000:]
    strict = r.stdout.split("adversarial gate, strict: FAIL", 1)[1]
    # every guard-stopped case is named by the strict check, including the ones
    # the suite's own gate scored safe because the database stopped the attack
    for c in bank.CASES:
        if c["id"] not in adversarial_gate.NOT_GUARD_STOPPED:
            assert c["id"] + " attempt" in strict, c["id"]


def test_every_case_has_an_attack_and_the_guard_refuses_the_dangerous_ones():
    import adversarial_gate
    import eval_bank_adversarial as bank
    import sql_guard

    assert set(adversarial_gate.ATTACKS) == {c["id"] for c in bank.CASES}
    for c in bank.CASES:
        if c.get("expect_guard", "").startswith("deny"):
            v = sql_guard.validate_query(adversarial_gate.ATTACKS[c["id"]])
            assert not v.allowed, c["id"]
