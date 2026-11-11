"""Security audit item 30 (SECURITY_STANDARDS 14.2): the agent's DuckDB
connections cannot reach files, the network or extensions, and generated SQL
cannot turn that back on -- even if the guard were bypassed. These tests talk
to the connection directly, under the guard, on purpose."""

import os
import sys

import duckdb
import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)

from analytics_query_tool import AnalyticsQueryTool, connect_agent_db  # noqa: E402
from data_backends import LocalDuckDBBackend  # noqa: E402

GOLD = os.path.join(ROOT, "medallion", "hospital_gold.duckdb")


def _connections():
    tool = AnalyticsQueryTool(db_path=GOLD, seed_demo=False)
    demo = AnalyticsQueryTool(seed_demo=True)  # :memory:, seeded before the lock
    yield "tool (file, read-only)", tool._con
    yield "tool (seeded :memory:)", demo._con
    yield "eval reference backend", LocalDuckDBBackend(GOLD).con


CONNECTIONS = list(_connections())


@pytest.mark.parametrize("name,con", CONNECTIONS, ids=[n for n, _ in CONNECTIONS])
def test_settings_are_off_and_locked(name, con):
    cur = con.cursor()
    for setting, want in (
        ("enable_external_access", "false"),
        ("autoinstall_known_extensions", "false"),
        ("autoload_known_extensions", "false"),
        ("lock_configuration", "true"),
    ):
        got = str(cur.execute(f"SELECT current_setting('{setting}')").fetchone()[0]).lower()
        assert got == want, (name, setting, got)


@pytest.mark.parametrize("name,con", CONNECTIONS, ids=[n for n, _ in CONNECTIONS])
@pytest.mark.parametrize(
    "sql",
    [
        "SET enable_external_access = true",
        "SET lock_configuration = false",
        "SET autoload_known_extensions = true",
    ],
)
def test_generated_sql_cannot_turn_it_back_on(name, con, sql):
    with pytest.raises(duckdb.Error):
        con.cursor().execute(sql)


@pytest.mark.parametrize("name,con", CONNECTIONS, ids=[n for n, _ in CONNECTIONS])
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM read_text('/proc/self/environ')",
        "SELECT * FROM read_csv('/etc/hostname')",
        "SELECT * FROM read_csv('http://127.0.0.1:9/x.csv')",
        "ATTACH '/tmp/claude-lockdown-test.duckdb' AS x",
    ],
)
def test_files_network_and_attach_are_refused_with_the_guard_bypassed(name, con, sql):
    with pytest.raises(duckdb.Error) as e:
        con.cursor().execute(sql).fetchall()
    assert "disabled" in str(e.value).lower() or "permission" in str(e.value).lower(), e.value


def test_the_tool_still_answers_through_the_guard():
    tool = AnalyticsQueryTool(db_path=GOLD, seed_demo=False)
    r = tool.execute({"sql": "SELECT count(*) AS n FROM gold_hospital_profile"})
    assert r.success and r.output["rows"][0]["n"] > 0, r.error


def test_lock_down_is_idempotent_on_an_already_locked_instance(tmp_path):
    p = str(tmp_path / "x.duckdb")
    duckdb.connect(p).close()
    a = connect_agent_db(p, read_only=True)
    b = connect_agent_db(p, read_only=True)  # same instance, already locked: no error
    assert b.execute("SELECT current_setting('lock_configuration')").fetchone()[0] in (True, "true")
    a.close()
    b.close()
