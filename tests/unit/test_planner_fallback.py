"""Backlog item 8 (with 14): Gemini by default, Groq when the provider is down.

No network: each provider's single call (`_complete_direct`) is replaced by a
script, so these tests decide what each provider does and check what the
planner makes of it.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import nl_to_sql_planner as planner_mod
from nl_to_sql_planner import CallMetrics, NLToSQLPlanner, PlannerError, is_provider_unavailable


class Status(Exception):
    def __init__(self, code, msg=""):
        super().__init__(msg or f"Error code: {code}")
        self.status_code = code


@pytest.fixture
def keys(monkeypatch):
    # dummy values: the calls themselves are scripted, nothing reaches a provider
    monkeypatch.setenv("GEMINI_API_KEY", "test-not-a-key")
    monkeypatch.setenv("GROQ_API_KEY", "test-not-a-key")
    monkeypatch.delenv("PLANNER_FALLBACK", raising=False)


def script(monkeypatch, behaviour):
    """behaviour: provider -> exception to raise, or None to answer."""
    calls = []

    def fake(self, system, user, role="plan"):
        calls.append(self.provider)
        err = behaviour.get(self.provider)
        if err is not None:
            raise err
        return "SELECT 1", CallMetrics(5, 10, 2, f"{self.provider}-model", self.provider)

    monkeypatch.setattr(NLToSQLPlanner, "_complete_direct", fake)
    return calls


def test_unset_provider_means_gemini():
    # a fresh interpreter: reloading the module here would swap the class the
    # other tests patch
    import subprocess
    # empty, not absent: the module's load_dotenv() never overrides a set
    # variable, so a developer .env pinning a provider cannot change the answer
    env = dict(os.environ, PLANNER_PROVIDER="")
    root = os.path.join(os.path.dirname(__file__), "..", "..")
    out = subprocess.run([sys.executable, "-c",
                          "import nl_to_sql_planner as p; print(p.ACTIVE_PROVIDER)"],
                         cwd=root, env=env, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "gemini"


def test_cerebras_is_gone_and_says_so(keys):
    assert "cerebras" not in planner_mod.PROVIDERS
    assert "cerebras" not in planner_mod.PROVIDER_RATES
    with pytest.raises(SystemExit, match="Cerebras was removed"):
        NLToSQLPlanner(provider="cerebras")


def test_an_unknown_provider_fails_with_a_clear_message(keys):
    with pytest.raises(SystemExit, match="unknown PLANNER_PROVIDER 'nope'"):
        NLToSQLPlanner(provider="nope")


def test_a_429_from_gemini_is_served_by_groq(keys, monkeypatch):
    calls = script(monkeypatch, {"gemini": Status(429, "Error code: 429 - quota exceeded")})
    p = NLToSQLPlanner(provider="gemini")
    sql, m = p.generate_sql_with_metrics("how many hospitals?")
    assert sql == "SELECT 1"
    assert calls == ["gemini", "groq"]
    assert m.provider == "groq" and m.fallback_from == "gemini"
    assert m.as_dict()["provider"] == "groq" and m.as_dict()["fallback_from"] == "gemini"


@pytest.mark.parametrize("code", [402, 500, 503])
def test_other_unavailable_codes_fall_back_too(keys, monkeypatch, code):
    calls = script(monkeypatch, {"gemini": Status(code)})
    _, m = NLToSQLPlanner(provider="gemini").generate_sql_with_metrics("q")
    assert calls == ["gemini", "groq"] and m.provider == "groq"


def test_the_critic_falls_back_as_well(keys, monkeypatch):
    calls = script(monkeypatch, {"gemini": Status(429)})
    text, m = NLToSQLPlanner(provider="gemini").critique("q", "SELECT 1")
    assert calls == ["gemini", "groq"] and m.fallback_from == "gemini"


def test_a_400_is_a_real_error_and_is_not_retried(keys, monkeypatch):
    calls = script(monkeypatch, {"gemini": Status(400, "Error code: 400 - bad request")})
    with pytest.raises(Status):
        NLToSQLPlanner(provider="gemini").generate_sql_with_metrics("q")
    assert calls == ["gemini"]


def test_a_403_config_error_is_not_hidden_behind_groq(keys, monkeypatch):
    # "insufficient" scores as an outage in the eval harness, but serving must
    # surface a permissions error, not answer it from another model
    calls = script(monkeypatch, {"gemini": Status(403, "insufficient permissions for this model")})
    with pytest.raises(Status):
        NLToSQLPlanner(provider="gemini").generate_sql_with_metrics("q")
    assert calls == ["gemini"]


def test_a_422_status_error_is_not_retried(keys, monkeypatch):
    calls = script(monkeypatch, {"gemini": Status(422, "unprocessable")})
    with pytest.raises(Status):
        NLToSQLPlanner(provider="gemini").generate_sql_with_metrics("q")
    assert calls == ["gemini"]


def test_a_connection_error_without_a_status_falls_back(keys, monkeypatch):
    class APIConnectionError(Exception):
        pass
    calls = script(monkeypatch, {"gemini": APIConnectionError("Connection error.")})
    _, m = NLToSQLPlanner(provider="gemini").generate_sql_with_metrics("q")
    assert calls == ["gemini", "groq"] and m.provider == "groq"


def test_record_mode_records_the_source_not_its_fallback(keys, monkeypatch):
    monkeypatch.setenv("ALLOW_REPLAY", "1")
    monkeypatch.setenv("E2E_RECORD", "1")
    monkeypatch.setenv("REPLAY_SOURCE_PROVIDER", "gemini")
    p = NLToSQLPlanner(provider="replay")
    assert p._source.provider == "gemini" and p._source.fallback_provider is None


def test_a_bad_key_is_not_retried(keys, monkeypatch):
    calls = script(monkeypatch, {"gemini": Status(401, "invalid api key")})
    with pytest.raises(Status):
        NLToSQLPlanner(provider="gemini").generate_sql_with_metrics("q")
    assert calls == ["gemini"]


def test_a_refusal_is_not_retried(keys, monkeypatch):
    calls = script(monkeypatch, {"gemini": PlannerError("model declined the request")})
    with pytest.raises(PlannerError):
        NLToSQLPlanner(provider="gemini").generate_sql_with_metrics("q")
    assert calls == ["gemini"]


def test_both_down_is_still_provider_unavailable(keys, monkeypatch):
    calls = script(monkeypatch, {"gemini": Status(429), "groq": Status(503)})
    with pytest.raises(Status) as err:
        NLToSQLPlanner(provider="gemini").generate_sql_with_metrics("q")
    assert calls == ["gemini", "groq"]
    assert is_provider_unavailable(err.value)          # the harness scores it as an outage
    assert err.value.__cause__ is not None              # and the first failure is kept


def test_fallback_none_turns_it_off(keys, monkeypatch):
    monkeypatch.setenv("PLANNER_FALLBACK", "none")
    calls = script(monkeypatch, {"gemini": Status(429)})
    p = NLToSQLPlanner(provider="gemini")
    assert p.fallback_provider is None
    with pytest.raises(Status):
        p.generate_sql_with_metrics("q")
    assert calls == ["gemini"]


def test_no_fallback_key_means_no_fallback_not_a_crash(keys, monkeypatch, capsys):
    monkeypatch.delenv("GROQ_API_KEY")
    calls = script(monkeypatch, {"gemini": Status(429)})
    p = NLToSQLPlanner(provider="gemini")
    assert p.fallback_provider is None
    assert "GROQ_API_KEY is not set" in capsys.readouterr().err
    with pytest.raises(Status):
        p.generate_sql_with_metrics("q")
    assert calls == ["gemini"]


def test_an_unknown_fallback_fails_at_startup(keys, monkeypatch):
    monkeypatch.setenv("PLANNER_FALLBACK", "cerebras")
    with pytest.raises(SystemExit, match="unknown PLANNER_FALLBACK"):
        NLToSQLPlanner(provider="gemini")


def test_groq_as_primary_has_no_fallback_to_itself(keys, monkeypatch):
    assert NLToSQLPlanner(provider="groq").fallback_provider is None


def test_a_healthy_call_records_no_fallback(keys, monkeypatch):
    calls = script(monkeypatch, {})
    _, m = NLToSQLPlanner(provider="gemini").generate_sql_with_metrics("q")
    assert calls == ["gemini"] and m.fallback_from is None
    assert "fallback_from" not in m.as_dict()


def test_the_eval_harness_never_falls_back():
    # a score measures one provider; the harness assigns this at import time
    import eval_harness  # noqa: F401
    assert os.environ["PLANNER_FALLBACK"] == "none"
