"""Unit tests for the Anthropic planner backend.

The Anthropic provider goes through the native SDK because current Claude
models changed the request contract: Haiku 5.5 rejects temperature/top_p/top_k
with a 400 and thinks by default, with thinking billed as output tokens. These
tests pin that contract without any network call -- the SDK client is replaced
with a stub that records what the planner sent.

The key used here is a dummy string; no real credential is involved.
"""

import importlib
from types import SimpleNamespace

import pytest

import nl_to_sql_planner as planner_mod
from eval_harness import _is_provider_error


class _StubMessages:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def _response(text="SELECT 1", stop_reason="end_turn", input_tokens=1200,
              output_tokens=300, stop_details=None, thinking=True):
    content = []
    if thinking:
        # thinking blocks carry no SQL and must be ignored
        content.append(SimpleNamespace(type="thinking", thinking=""))
    if text is not None:
        content.append(SimpleNamespace(type="text", text=text))
    return SimpleNamespace(
        content=content, stop_reason=stop_reason, stop_details=stop_details,
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
    )


@pytest.fixture
def no_pacing(monkeypatch):
    # a local .env may pin PLANNER_MIN_INTERVAL_SEC=5; tests must not sleep
    monkeypatch.setenv("PLANNER_MIN_INTERVAL_SEC", "0")


@pytest.fixture
def planner(monkeypatch, no_pacing):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-dummy-not-a-key")
    p = planner_mod.NLToSQLPlanner(provider="anthropic")
    stub = _StubMessages(_response())
    p._client = SimpleNamespace(messages=stub)
    return p, stub


@pytest.fixture
def reload_planner(monkeypatch):
    """Reload the module under a changed env, and restore it afterwards.

    PROVIDERS is built at import time (like $GEMINI_MODEL), so testing the
    $ANTHROPIC_MODEL default/override needs a reload.
    """
    def _reload():
        return importlib.reload(planner_mod)
    yield _reload
    monkeypatch.undo()
    importlib.reload(planner_mod)


# --- model selection --------------------------------------------------------

def test_default_model_is_haiku_5_5(monkeypatch, reload_planner):
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    mod = reload_planner()
    assert mod.PROVIDERS["anthropic"]["model"] == "claude-haiku-5-5"


def test_anthropic_model_env_overrides_default(monkeypatch, reload_planner):
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")
    mod = reload_planner()
    assert mod.PROVIDERS["anthropic"]["model"] == "claude-sonnet-5-5"


def test_deprecated_sonnet_4_5_is_not_configured():
    models = {cfg["model"] for cfg in planner_mod.PROVIDERS.values()}
    assert "claude-sonnet-4-5" not in models


def test_missing_key_refuses_to_construct(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(SystemExit, match="ANTHROPIC_API_KEY"):
        planner_mod.NLToSQLPlanner(provider="anthropic")


# --- request shape ----------------------------------------------------------

def test_request_sends_no_sampling_params(planner):
    p, stub = planner
    p.generate_sql_with_metrics("How many hospitals are there?")
    sent = stub.calls[0]
    for banned in ("temperature", "top_p", "top_k"):
        assert banned not in sent, f"{banned} returns a 400 on current Claude models"


def test_request_sets_low_effort_and_room_for_thinking(planner):
    p, stub = planner
    p.generate_sql_with_metrics("How many hospitals are there?")
    sent = stub.calls[0]
    assert sent["output_config"] == {"effort": "low"}
    assert sent["max_tokens"] >= 4000
    assert "thinking" not in sent       # adaptive is the default; effort bounds it
    assert sent["model"] == p._model


def test_request_carries_schema_prompt_and_question(planner):
    p, stub = planner
    p.generate_sql_with_metrics("Which hospitals are cheapest?",
                                schema_doc=planner_mod.SCHEMA_DOC_FHIR)
    sent = stub.calls[0]
    assert "gold_patient" in sent["system"]
    assert sent["messages"] == [{"role": "user", "content": "Which hospitals are cheapest?"}]


# --- response handling ------------------------------------------------------

def test_sql_comes_from_text_blocks_only(planner):
    p, stub = planner
    stub.response = _response(text="```sql\nSELECT count(*) AS n FROM gold_hospital_profile;\n```")
    sql, _ = p.generate_sql_with_metrics("How many hospitals?")
    assert sql == "SELECT count(*) AS n FROM gold_hospital_profile"


def test_metrics_read_usage_and_price_at_model_rate(planner):
    p, stub = planner
    stub.response = _response(input_tokens=1_000_000, output_tokens=1_000_000)
    _, m = p.generate_sql_with_metrics("q")
    assert (m.prompt_tokens, m.completion_tokens) == (1_000_000, 1_000_000)
    assert m.provider == "anthropic"
    rate = planner_mod.rate_for("anthropic", p._model)
    assert m.est_cost_usd == pytest.approx(rate["input"] + rate["output"])


def test_refusal_raises_planner_error_with_category(planner):
    p, stub = planner
    stub.response = _response(text=None, stop_reason="refusal",
                              stop_details=SimpleNamespace(category="cyber"))
    with pytest.raises(planner_mod.PlannerError, match="refusal: cyber"):
        p.generate_sql_with_metrics("q")


def test_truncated_output_is_rejected_not_executed(planner):
    p, stub = planner
    stub.response = _response(text="SELECT facility_name FROM gold_hos",
                              stop_reason="max_tokens")
    with pytest.raises(planner_mod.PlannerError, match="truncated"):
        p.generate_sql_with_metrics("q")


def test_planner_errors_score_as_model_errors_not_outages():
    # a refusal or truncation is about the model's answer, not provider uptime
    assert not _is_provider_error(planner_mod.PlannerError(
        "model declined the request (refusal: cyber)"))
    assert not _is_provider_error(planner_mod.PlannerError(
        "model output truncated at max_tokens=4000"))


# --- rates ------------------------------------------------------------------

def test_model_rate_beats_provider_rate():
    assert planner_mod.rate_for("anthropic", "claude-opus-5-5") == {"input": 4.00, "output": 20.00}
    assert planner_mod.rate_for("anthropic", "claude-haiku-5-5") == {"input": 0.10, "output": 0.50}


def test_unlisted_model_falls_back_to_provider_rate():
    assert planner_mod.rate_for("gemini", "some-new-model") == planner_mod.PROVIDER_RATES["gemini"]
    assert planner_mod.rate_for("nobody", "x") == planner_mod._DEFAULT_RATE
