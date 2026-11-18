"""
state_db.py
===========
Where durable state lives (backlog item 19): Postgres when STATE_DB_URL is a
postgres:// URL, otherwise the SQLite files used today -- so local runs and the
test suite are unchanged without it.

On Render's free plan the disk does not last, so an approval queued before a
restart or redeploy was lost with the SQLite file. With STATE_DB_URL pointing
at a hosted Postgres (a free tier the owner creates; item 19, "ask"), the queue,
its decision history and the graph checkpoints survive.

What this module covers so far: the approval store (approval.py) and the
LangGraph checkpointers of both graphs. Traces, the answer cache, the audit log
and usage still use their SQLite files (later PRs under item 19).

The adapter is deliberately thin. The stores keep their SQL, written with `?`
placeholders; for Postgres they become `%s`, rows come back as dicts (sqlite3's
Row also answers row["col"]), and the few DDL differences are handled by the
callers through `dialect`.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
from typing import Any, Iterator, Optional


def url() -> str:
    return (os.environ.get("STATE_DB_URL") or "").strip()


def is_postgres(u: Optional[str] = None) -> bool:
    u = url() if u is None else u
    return u.startswith(("postgres://", "postgresql://"))


_LOCAL_HOSTS = {"", "localhost", "127.0.0.1", "::1"}


_STRONG_MODES = {"require", "verify-ca", "verify-full"}


def tls_kwargs(u: Optional[str] = None) -> dict[str, str]:
    """Backlog item 61: require TLS to a Postgres that is not on this machine.

    `sslmode=require` is added unless the URL already names an `sslmode`, or
    libpq's own PGSSLMODE is set, or EVERY host is local (a unix socket,
    localhost, 127.0.0.1 or ::1: CI's service container). The URL is read the
    way libpq reads it, so a remote `host=`/`hostaddr=` in the query string
    counts (review of #156). STATE_DB_SSLMODE picks a stronger mode
    (`verify-ca`, `verify-full`); anything else falls back to `require` --
    weakening TLS for a remote host is the URL's to state, explicitly."""
    u = url() if u is None else u
    try:
        from psycopg.conninfo import conninfo_to_dict

        params = {k.lower(): str(v) for k, v in conninfo_to_dict(u).items() if v is not None}
    except Exception:  # noqa: BLE001 -- unparseable or no psycopg: require TLS
        params = {}
        hosts = ["?"]
    else:
        hosts = [
            h.strip()
            for key in ("host", "hostaddr")
            for h in params.get(key, "").split(",")
            if h.strip()
        ]
    if params.get("sslmode") or os.environ.get("PGSSLMODE"):
        return {}
    if all(h in _LOCAL_HOSTS or h.startswith("/") for h in hosts):
        return {}  # no host at all is a local unix socket
    mode = os.environ.get("STATE_DB_SSLMODE", "").strip().lower()
    return {"sslmode": mode if mode in _STRONG_MODES else "require"}


def describe() -> str:
    """For /health: which store, without the credentials in the URL."""
    # /health is unauthenticated: name the kind of store, not its host
    return "postgres" if is_postgres() else "sqlite"


class _PGConn:
    """The sqlite3 surface the stores use (execute -> cursor), over psycopg."""

    dialect = "postgres"

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def execute(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> Any:
        from psycopg.rows import dict_row

        cur = self._conn.cursor(row_factory=dict_row)
        # the stores write sqlite-style `?`; psycopg wants `%s`, so a literal %
        # must be doubled first
        cur.execute(sql.replace("%", "%%").replace("?", "%s"), tuple(params))
        return cur


class _SQLiteConn:
    dialect = "sqlite"

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def execute(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> Any:
        return self._conn.execute(sql, tuple(params))


@contextlib.contextmanager
def connect(sqlite_path: str) -> Iterator[Any]:
    """One transaction: committed on success, rolled back on an error, closed."""
    if is_postgres():
        import psycopg

        pg = psycopg.connect(url(), **tls_kwargs())
        try:
            yield _PGConn(pg)
            pg.commit()
        except BaseException:
            pg.rollback()
            raise
        finally:
            pg.close()
        return
    os.makedirs(os.path.dirname(sqlite_path) or ".", exist_ok=True)
    con = sqlite3.connect(sqlite_path)
    con.row_factory = sqlite3.Row
    try:
        yield _SQLiteConn(con)
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()


def columns(con: Any, table: str) -> set[str]:
    """The column names of a table, in either dialect."""
    if con.dialect == "postgres":
        rows = con.execute(
            "SELECT column_name AS name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = ?",
            (table,),
        ).fetchall()
    else:
        rows = con.execute(f"PRAGMA table_info({table})").fetchall()
    return {r["name"] for r in rows}


def autoincrement_pk(con: Any) -> str:
    return (
        "BIGSERIAL PRIMARY KEY"
        if con.dialect == "postgres"
        else "INTEGER PRIMARY KEY AUTOINCREMENT"
    )


def checkpointer(sqlite_path: str) -> Any:
    """A LangGraph checkpointer context manager: PostgresSaver on STATE_DB_URL
    (tables created on first use), else SqliteSaver on the given file."""
    if is_postgres():
        from langgraph.checkpoint.postgres import PostgresSaver

        @contextlib.contextmanager
        def _pg() -> Iterator[Any]:
            # A pool, not one long-lived connection: free hosted plans drop
            # idle connections, and a dead one would fail every graph run until
            # a restart. check= revalidates a connection before handing it out.
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool

            pool = ConnectionPool(
                url(),
                min_size=1,
                max_size=4,
                kwargs={
                    "autocommit": True,
                    "prepare_threshold": 0,
                    "row_factory": dict_row,
                    **tls_kwargs(),
                },
                check=ConnectionPool.check_connection,
                open=True,
            )
            try:
                saver = PostgresSaver(pool)
                saver.setup()
                yield saver
            finally:
                pool.close()

        return _pg()
    from langgraph.checkpoint.sqlite import SqliteSaver

    return SqliteSaver.from_conn_string(sqlite_path)
