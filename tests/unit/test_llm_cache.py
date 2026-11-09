"""Backlog item 6: a repeated question skips the LLM; nothing stale is served.

The cache holds the model's TEXT only. A hit still goes through the guard and
runs on today's data, so these tests check: one model call for two identical
requests, a different prompt / provider / data vintage / guard version is a
miss, a hit's metrics say cached and cost nothing, /metrics shows the hit rate,
and the eval harness never caches.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import llm_cache
from nl_to_sql_planner import CallMetrics, NLToSQLPlanner


@pytest.fixture
def on(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_CACHE", "1")
    monkeypatch.setenv("GEMINI_API_KEY", "test-not-a-key")
    monkeypatch.setenv("PLANNER_FALLBACK", "none")
    cache = llm_cache.LLMCache(str(tmp_path / "c.sqlite"))
    llm_cache.set_cache(cache)
    calls = []

    def fake(self, system, user, role="plan"):
        calls.append((role, user))
        return f"SELECT {len(calls)}", CallMetrics(9, 100, 10, "m", self.provider)

    monkeypatch.setattr(NLToSQLPlanner, "_complete_direct", fake)
    return cache, calls


def test_asking_twice_makes_one_model_call(on):
    cache, calls = on
    p = NLToSQLPlanner(provider="gemini")
    a, m1 = p.generate_sql_with_metrics("how many hospitals?")
    b, m2 = p.generate_sql_with_metrics("how many hospitals?")
    assert a == b == "SELECT 1" and len(calls) == 1
    assert not m1.cached and m2.cached
    assert m2.est_cost_usd == 0 and m2.total_tokens == 0
    assert m2.as_dict()["cached"] is True and "cached" not in m1.as_dict()
    assert cache.stats()["hits"] == 1 and cache.stats()["misses"] == 1
    assert cache.stats()["hit_rate"] == 0.5


def test_the_critic_is_cached_too(on):
    _, calls = on
    p = NLToSQLPlanner(provider="gemini")
    p.critique("q", "SELECT 1")
    p.critique("q", "SELECT 1")
    assert len(calls) == 1


def test_a_different_question_or_schema_is_a_miss(on):
    _, calls = on
    p = NLToSQLPlanner(provider="gemini")
    p.generate_sql_with_metrics("a?")
    p.generate_sql_with_metrics("b?")
    p.generate_sql_with_metrics("a?", schema_doc="TABLE x(a INT)")  # another prompt
    assert len(calls) == 3


def test_a_data_refresh_or_guard_change_starts_cold(on, monkeypatch):
    _, calls = on
    p = NLToSQLPlanner(provider="gemini")
    p.generate_sql_with_metrics("q?")
    monkeypatch.setattr(llm_cache, "data_vintage", lambda: "2099-01-01")
    p.generate_sql_with_metrics("q?")
    monkeypatch.setattr(llm_cache, "guard_version", lambda: "changed")
    p.generate_sql_with_metrics("q?")
    assert len(calls) == 3


def test_entries_expire(on, tmp_path, monkeypatch):
    _, calls = on
    llm_cache.set_cache(llm_cache.LLMCache(str(tmp_path / "ttl.sqlite"), ttl_sec=1))
    p = NLToSQLPlanner(provider="gemini")
    p.generate_sql_with_metrics("q?")
    import time

    real = time.time
    monkeypatch.setattr(llm_cache.time, "time", lambda: real() + 5)
    p.generate_sql_with_metrics("q?")
    assert len(calls) == 2


def test_off_means_every_request_asks_the_model(on, monkeypatch):
    _, calls = on
    monkeypatch.setenv("LLM_CACHE", "0")
    p = NLToSQLPlanner(provider="gemini")
    p.generate_sql_with_metrics("q?")
    p.generate_sql_with_metrics("q?")
    assert len(calls) == 2


def test_a_broken_store_is_a_miss_not_an_error(on, monkeypatch):
    _, calls = on
    llm_cache.set_cache(llm_cache.LLMCache("/proc/no/such/dir/c.sqlite"))
    p = NLToSQLPlanner(provider="gemini")
    p.generate_sql_with_metrics("q?")
    p.generate_sql_with_metrics("q?")
    assert len(calls) == 2


def test_the_eval_harness_never_caches():
    import eval_harness  # noqa: F401

    assert os.environ["LLM_CACHE"] == "0"


def test_the_store_holds_no_rows(on, tmp_path):
    cache, _ = on
    p = NLToSQLPlanner(provider="gemini")
    p.generate_sql_with_metrics("q?")
    import sqlite3

    cols = [r[1] for r in sqlite3.connect(cache.path).execute("PRAGMA table_info(answers)")]
    assert cols == ["key", "role", "text", "provider", "model", "created"]
