"""state_db (item 19): what the tests may and may not touch, and the adapter."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import state_db


def test_the_suite_never_sees_a_real_state_db_url():
    # tests/conftest.py removes STATE_DB_URL for every test (the app loads .env
    # on import, and the suite drops tables); only TEST_STATE_DB_URL may stand in
    assert os.environ.get("STATE_DB_URL") in (None, os.environ.get("TEST_STATE_DB_URL"))


def test_unset_means_sqlite(monkeypatch):
    monkeypatch.delenv("STATE_DB_URL", raising=False)
    assert not state_db.is_postgres() and state_db.describe() == "sqlite"


def test_health_never_shows_the_url(monkeypatch):
    monkeypatch.setenv("STATE_DB_URL", "postgresql://u:p@a@db.example.com/prod")
    assert state_db.describe() == "postgres"


def test_sqlite_round_trip(tmp_path, monkeypatch):
    monkeypatch.delenv("STATE_DB_URL", raising=False)
    path = str(tmp_path / "x" / "s.sqlite")
    with state_db.connect(path) as con:
        con.execute(f"CREATE TABLE t (id {state_db.autoincrement_pk(con)}, v TEXT)")
        con.execute("INSERT INTO t (v) VALUES (?)", ("a%b",))
    with state_db.connect(path) as con:
        assert con.execute("SELECT v FROM t").fetchone()["v"] == "a%b"
        assert state_db.columns(con, "t") == {"id", "v"}


def test_an_error_rolls_back(tmp_path, monkeypatch):
    monkeypatch.delenv("STATE_DB_URL", raising=False)
    path = str(tmp_path / "r.sqlite")
    with state_db.connect(path) as con:
        con.execute("CREATE TABLE t (v TEXT)")
    try:
        with state_db.connect(path) as con:
            con.execute("INSERT INTO t VALUES (?)", ("x",))
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    with state_db.connect(path) as con:
        assert con.execute("SELECT count(*) AS n FROM t").fetchone()["n"] == 0
