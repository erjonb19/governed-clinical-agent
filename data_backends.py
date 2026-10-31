"""
data_backends.py
================
Backend-agnostic data layer for the governed agent. The guard, policy, and
planner sit ABOVE this, unchanged, so governance is identical no matter where
the Gold lives. One config switch picks the path:

    DATA_BACKEND=local       -> LocalDuckDBBackend  (default, what the deploy uses)
    DATA_BACKEND=databricks  -> DatabricksBackend   (the cloud lift)

Every backend takes already-validated SQL (the guard's safe_sql) and returns
(columns, rows) where rows are list[dict]. Nothing here bypasses the guard;
backends only execute SQL the runtime already approved.
"""

from __future__ import annotations

import os
from typing import Any, Protocol


class QueryBackend(Protocol):
    kind: str
    def execute(self, sql: str) -> tuple[list[str], list[dict]]: ...


class LocalDuckDBBackend:
    """Runs validated SQL against a local DuckDB file (or :memory:)."""
    kind = "local-duckdb"

    def __init__(self, db_path: str = ":memory:"):
        import duckdb
        self.db_path = db_path
        # read-only, like AnalyticsQueryTool's connection: it only runs validated
        # SELECTs, and DuckDB refuses two connections to one file in one process
        # unless their configuration matches (the eval harness holds both)
        self.con = duckdb.connect(db_path, read_only=db_path != ":memory:")

    def execute(self, sql: str) -> tuple[list[str], list[dict]]:
        cur = self.con.execute(sql)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        return cols, rows


class DatabricksBackend:
    """
    Runs the SAME validated SQL against Delta tables on Databricks SQL.
    Implemented for the cloud lift; reads connection details from the env:
        DATABRICKS_SERVER_HOSTNAME, DATABRICKS_HTTP_PATH, DATABRICKS_TOKEN
        DATABRICKS_CATALOG (default 'main'), DATABRICKS_SCHEMA (default 'gold')

    VERIFIED end-to-end against a real workspace (Databricks Free Edition
    serverless SQL): Gold published to Delta, agent queries served with the guard
    intact, and the eval subset passing 100% on both backends (parity proven).
    The local/Render deploy still defaults to DuckDB, so switching backends is a
    config change, not a rewrite.
    Requires: pip install databricks-sql-connector==4.4.0
    """
    kind = "databricks-delta"

    def __init__(self):
        self._host = os.environ.get("DATABRICKS_SERVER_HOSTNAME")
        self._http_path = os.environ.get("DATABRICKS_HTTP_PATH")
        self._token = os.environ.get("DATABRICKS_TOKEN")
        self._catalog = os.environ.get("DATABRICKS_CATALOG", "main")
        self._schema = os.environ.get("DATABRICKS_SCHEMA", "gold")
        if not all([self._host, self._http_path, self._token]):
            raise SystemExit(
                "DatabricksBackend needs DATABRICKS_SERVER_HOSTNAME, "
                "DATABRICKS_HTTP_PATH, and DATABRICKS_TOKEN in the environment."
            )

    def execute(self, sql: str) -> tuple[list[str], list[dict]]:
        from databricks import sql as dbsql  # lazy import
        with dbsql.connect(
            server_hostname=self._host,
            http_path=self._http_path,
            access_token=self._token,
            catalog=self._catalog,
            schema=self._schema,
        ) as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                cols = [d[0] for d in cur.description] if cur.description else []
                rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        return cols, rows


def get_backend(db_path: str | None = None) -> QueryBackend:
    """Factory. Reads DATA_BACKEND; defaults to local DuckDB."""
    kind = os.environ.get("DATA_BACKEND", "local").lower()
    if kind == "databricks":
        return DatabricksBackend()
    path = db_path or os.environ.get("HOSPITAL_GOLD_DB", "medallion/hospital_gold.duckdb")
    return LocalDuckDBBackend(path)
