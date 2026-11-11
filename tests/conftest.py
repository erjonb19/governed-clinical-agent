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
            {
                "name": "filesystem.read",
                "allowed": True,
                "constraints": {"paths": {"allow": ["/workspace/**"], "deny": []}},
            },
            {"name": "shell.execute", "allowed": False},
        ],
    }


@pytest.fixture(autouse=True)
def _fresh_postgres_state(monkeypatch):
    """Tests NEVER use STATE_DB_URL (item 19). The app loads a .env on import,
    so a real database URL there would otherwise receive -- and have dropped --
    every test's state. Only TEST_STATE_DB_URL (set by the CI job's Postgres
    service container) puts the suite on Postgres, and only on a database whose
    name contains "test"; each test then starts from empty state tables, as it
    does with a fresh SQLite file under tmp_path."""
    import os

    monkeypatch.delenv("STATE_DB_URL", raising=False)
    u = os.environ.get("TEST_STATE_DB_URL", "")
    if not u.startswith(("postgres://", "postgresql://")):
        yield
        return
    dbname = u.rsplit("/", 1)[-1].split("?", 1)[0]
    if "test" not in dbname.lower():
        raise RuntimeError(
            f"TEST_STATE_DB_URL must name a database containing 'test' "
            f"(got {dbname!r}); the suite drops its tables"
        )
    monkeypatch.setenv("STATE_DB_URL", u)
    import psycopg

    with psycopg.connect(u, autocommit=True) as con:
        con.execute(
            "DROP TABLE IF EXISTS approval_events, approvals, checkpoints, "
            "checkpoint_blobs, checkpoint_writes, checkpoint_migrations CASCADE"
        )
    yield


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


@pytest.fixture(autouse=True)
def _fresh_llm_budget():
    """The daily model budget is process-wide (item 37): every test starts with
    a fresh, generous day, so the suite's many /query calls never exhaust it."""
    import llm_budget

    b = llm_budget.BUDGET
    saved = (b.max_runs, b.max_usd, b.now)
    b.max_runs, b.max_usd = 1_000_000, 1_000_000.0
    b._day = ""
    yield
    b.max_runs, b.max_usd, b.now = saved
    b._day = ""


@pytest.fixture(autouse=True)
def _fresh_auth_failures():
    """Failed-key counts are process-wide (item 35); every test starts from none,
    so one test's deliberate wrong keys never throttle the next one's."""
    import auth_failures

    f = auth_failures.FAILURES
    saved = (f.per_window, f.window_sec, f.lockout, f.clock, f.audit)
    f.reset()
    f.lockout = False
    yield
    f.per_window, f.window_sec, f.lockout, f.clock, f.audit = saved
    f.reset()
