"""
Pytest fixtures shared across tests (plan §4.1, §4.2).

Fixtures: paths to example policy files, minimal policy dicts.
"""

import pytest
from pathlib import Path


# Repo root (parent of tests/)
REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def repo_root() -> Path:
    """Return the repository root directory."""
    return REPO_ROOT


@pytest.fixture
def policy_yaml_path(repo_root: Path) -> Path:
    """Path to examples/policies/Policy.yaml."""
    return repo_root / "examples" / "policies" / "Policy.yaml"


@pytest.fixture
def policy_json_path(repo_root: Path) -> Path:
    """Path to examples/policies/Policy.json."""
    return repo_root / "examples" / "policies" / "Policy.json"


@pytest.fixture
def tool_definitions_json_path(repo_root: Path) -> Path:
    """Path to examples/policies/tool_definitions.json."""
    return repo_root / "examples" / "policies" / "tool_definitions.json"


@pytest.fixture
def minimal_policy() -> dict:
    """Minimal valid policy dict for tests."""
    return {
        "version": "1.0",
        "default_policy": "deny",
        "capabilities": [
            {"name": "filesystem.read", "allowed": True, "constraints": {"paths": {"allow": ["/workspace/**"], "deny": []}}},
            {"name": "shell.execute", "allowed": False},
        ],
    }


@pytest.fixture(autouse=True)
def _no_answer_cache_by_default(tmp_path, monkeypatch):
    """The answer cache (llm_cache.py, item 6) is off in tests unless a test
    turns it on, and it never writes into the repo's logs/: one test's model
    answer must not answer the next test's request."""
    monkeypatch.setenv("LLM_CACHE", "0")
    monkeypatch.setenv("LLM_CACHE_DB", str(tmp_path / "llm_cache.sqlite"))
    try:
        import llm_cache
        llm_cache.set_cache(None)
    except ImportError:
        pass
    yield
    try:
        import llm_cache
        llm_cache.set_cache(None)
    except ImportError:
        pass
