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
def _fresh_postgres_state():
    """With STATE_DB_URL set (the CI job that runs the suite against a Postgres
    service container, item 19), every test starts from empty state tables, as
    it does with a fresh SQLite file under tmp_path."""
    import os
    u = os.environ.get("STATE_DB_URL", "")
    if not u.startswith(("postgres://", "postgresql://")):
        yield
        return
    import psycopg
    with psycopg.connect(u, autocommit=True) as con:
        con.execute("DROP TABLE IF EXISTS approval_events, approvals, checkpoints, "
                    "checkpoint_blobs, checkpoint_writes, checkpoint_migrations CASCADE")
    yield
