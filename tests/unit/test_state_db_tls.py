"""Backlog item 61: a remote Postgres is reached over TLS unless the URL says otherwise."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
import state_db  # noqa: E402

REMOTE = "postgresql://u:p@db.example.com:5432/state"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("PGSSLMODE", raising=False)
    monkeypatch.delenv("STATE_DB_SSLMODE", raising=False)


def test_a_remote_host_requires_tls():
    assert state_db.tls_kwargs(REMOTE) == {"sslmode": "require"}


@pytest.mark.parametrize(
    "u",
    [
        "postgresql://ci:ci@localhost:5432/state_test",
        "postgresql://ci:ci@127.0.0.1/state_test",
        "postgresql://ci:ci@[::1]/state_test",
        "postgresql:///state_test",  # unix socket
    ],
)
def test_a_local_host_is_left_alone(u):
    assert state_db.tls_kwargs(u) == {}


def test_the_url_s_own_sslmode_wins():
    assert state_db.tls_kwargs(REMOTE + "?sslmode=verify-full") == {}
    assert state_db.tls_kwargs(REMOTE + "?application_name=x&sslmode=disable") == {}


@pytest.mark.parametrize(
    "u",
    [
        "postgresql://u:p@/db?host=db.example.com",  # host in the query string
        "postgresql://u:p@localhost/db?hostaddr=10.0.0.5",  # hostaddr wins in libpq
        "postgres://u:p@db.example.com/db",
        "postgresql://u:p@localhost,db.example.com/db",  # any remote host
    ],
)
def test_remote_hosts_in_any_form_require_tls(u):
    assert state_db.tls_kwargs(u) == {"sslmode": "require"}


def test_a_weaker_state_db_sslmode_is_ignored(monkeypatch):
    monkeypatch.setenv("STATE_DB_SSLMODE", "disable")
    assert state_db.tls_kwargs(REMOTE) == {"sslmode": "require"}


def test_pgsslmode_and_state_db_sslmode(monkeypatch):
    monkeypatch.setenv("STATE_DB_SSLMODE", "verify-full")
    assert state_db.tls_kwargs(REMOTE) == {"sslmode": "verify-full"}
    monkeypatch.setenv("PGSSLMODE", "require")
    assert state_db.tls_kwargs(REMOTE) == {}


def test_connect_passes_it_to_psycopg(monkeypatch):
    psycopg = pytest.importorskip("psycopg")
    seen = {}

    class Fake:
        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass

    def fake_connect(conninfo, **kw):
        seen.update(kw, conninfo=conninfo)
        return Fake()

    monkeypatch.setenv("STATE_DB_URL", REMOTE)
    monkeypatch.setattr(psycopg, "connect", fake_connect)
    with state_db.connect("unused.sqlite"):
        pass
    assert seen["sslmode"] == "require" and seen["conninfo"] == REMOTE


def test_the_checkpointer_pool_gets_it_too(monkeypatch):
    pool_mod = pytest.importorskip("psycopg_pool")
    pytest.importorskip("langgraph.checkpoint.postgres")
    seen = {}

    class FakePool:
        check_connection = staticmethod(lambda c: None)

        def __init__(self, conninfo, **kw):
            seen.update(kw.get("kwargs", {}))
            raise RuntimeError("stop here")

    monkeypatch.setenv("STATE_DB_URL", REMOTE)
    monkeypatch.setattr(pool_mod, "ConnectionPool", FakePool)
    with pytest.raises(RuntimeError):
        with state_db.checkpointer("unused.sqlite"):
            pass
    assert seen["sslmode"] == "require"
