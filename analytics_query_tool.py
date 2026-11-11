"""Analytics query tool (capability: analytics.query_aggregate).

The agent writes its own SQL for flexibility. Before anything runs, the SQL is
parsed and checked by sql_guard.validate_query: SELECT only, Gold table
allowlist, row cap, no catalog access, no file-reading functions. A denied
query returns a clean ToolResult failure; an allowed query runs the validator's
safe_sql (row cap already applied) and returns rows.

Two layers govern this tool:
  1. The policy engine decides whether analytics.query_aggregate may run at all.
  2. sql_guard decides whether THIS specific query is safe.
Plus the DB backstop: point this at a read-only role / PHI-excluding views.

Demo mode: with no real Gold warehouse, the tool seeds a tiny in-memory DuckDB
so allowed queries return real rows. For production, pass db_path to your real
DuckDB/Parquet store and set seed_demo=False.
"""

from __future__ import annotations

import re
import os
import threading
from typing import Any, Dict

import duckdb

from src.tools.base import BaseTool, ToolResult
import phi_guard
from sql_guard import validate_query




_QUOTED_VALUE = re.compile(r"'(?:[^']|'')*'")

# Errors whose message is ABOUT A DATA VALUE. DuckDB quotes the value -- in single
# quotes, in double quotes, or bare on its own line under a caret -- so their
# text cannot be kept at all. Databricks says the same things in its own words.
_DATA_ERROR_TYPES = {"ConversionException", "InvalidInputException", "OutOfRangeException"}
_DATA_ERROR_WORDS = ("could not convert", "could not parse", "conversion error",
                     "cast_invalid_input", "invalid_format", "out of range", "out_of_range",
                     "out_of_bounds", "cannot be cast", "malformed")
# Databricks / ANSI SQL put the SQLSTATE in the message; class 22 is "data
# exception" -- whatever the error class is called, it is about a value.
_SQLSTATE_DATA = re.compile(r"SQLSTATE[:=\s]*22[0-9A-Z]{3}", re.I)
# The target type is the LAST thing on the message's first line ("... to INT32"),
# after the quoted value; anchoring there keeps a value that happens to contain
# " to WORD" from being read as the type.
_TARGET_TYPE = re.compile(r"\bto (?:type )?([A-Z][A-Z0-9_]*(?:\(\d+(?:,\s*\d+)?\))?)\s*$")


def redact_values(message: str) -> str:
    """Every single-quoted literal in `message` replaced (the fallback for
    errors that are about the SQL, whose LINE 1 echo can still carry a literal)."""
    return _QUOTED_VALUE.sub("'<value>'", message)


# Per-statement time limit (twelfth review, case 6). The guard rules on what a
# query may RETURN, not what it costs: a self-join or cross join of a patient
# table passes it. Measured on the real Gold's size (223,503 synthetic
# observations): a key self-join 0.31 s, a self-join on a safe column 22 s, a
# three-way self-join and a cross join still running at 60 s
# (docs/measurements/compute_bomb_2026-11-03.txt). 10 s is ~30x the slowest
# legitimate shape measured, and stops the bombs.
DEFAULT_QUERY_TIMEOUT_SEC = 10.0


def query_timeout() -> float:
    """QUERY_TIMEOUT_SEC, a positive number of seconds; anything else is the
    default. There is deliberately no "off" value."""
    try:
        v = float(os.environ.get("QUERY_TIMEOUT_SEC", DEFAULT_QUERY_TIMEOUT_SEC))
    except ValueError:
        return DEFAULT_QUERY_TIMEOUT_SEC
    return v if v > 0 else DEFAULT_QUERY_TIMEOUT_SEC


class QueryTimeout(Exception):
    def __init__(self, seconds: float):
        super().__init__(f"query timed out after {seconds:g} s (QUERY_TIMEOUT_SEC) and was "
                         f"stopped; write a cheaper query -- avoid self-joins and cross joins "
                         f"of large tables")


def _error_type(text: str | None) -> str | None:
    """'IOException: Could not set lock on file "/srv/x.duckdb"' -> 'IOException'."""
    if not text:
        return text
    head = text.split(":", 1)[0].strip()
    return head if head.isidentifier() else "error"


def run_with_timeout(con, sql: str, seconds: float,
                     max_rows: int | None = None) -> tuple[list, list]:
    """Execute and fetch on a DuckDB connection, interrupting it after
    `seconds`. DuckDB has no statement_timeout setting, so a timer calls
    con.interrupt(). `con` is a cursor of its own for this one execution
    (AnalyticsQueryTool.execute, backlog item 20), so the interrupt stops this
    query and nobody else's; the timer is disarmed under a lock before
    returning, and an interrupt that lands on an idle connection does not
    affect the next query (tests/unit/test_query_timeout.py)."""
    lock, done, fired = threading.Lock(), [False], [False]

    def fire():
        with lock:
            if not done[0]:
                fired[0] = True
                con.interrupt()

    timer = threading.Timer(seconds, fire)
    timer.daemon = True
    timer.start()
    try:
        cur = con.execute(sql)
        cols = [d[0] for d in cur.description] if cur.description else []
        # max_rows (item 43): fetch at most one row past the cap. The guard adds
        # or clamps a LIMIT on a plain SELECT, but not on a UNION / set
        # operation, and fetchall() would pull all of it into memory.
        raw = cur.fetchall() if max_rows is None else cur.fetchmany(max_rows + 1)
        rows = [dict(zip(cols, r)) for r in raw]
        return cols, rows
    except duckdb.InterruptException:
        if fired[0]:
            raise QueryTimeout(seconds) from None
        raise
    finally:
        with lock:
            done[0] = True
        timer.cancel()


def describe_execution_error(e: Exception) -> str:
    """An execution error safe to show anyone, and to send back to the LLM.

    The text goes to the client, the trace, the graph's checkpoint and -- on a
    retry -- the next prompt to the provider, so it must not carry data (hard
    rule 3). The PHI at risk is FHIR's birth_date, city and postal_code: a
    model that casts or parses one wrongly gets an error quoting a real value.

    So an error about a VALUE is reduced to its class and the target type,
    which is all the agent needs to fix its SQL ("Conversion Error converting to
    DATE: the value is withheld"). Errors about the SQL itself (binder, parser,
    catalog) keep their text, which names columns and tables, with any quoted
    literal redacted.
    """
    name = type(e).__name__
    text = str(e)
    if (name in _DATA_ERROR_TYPES or _SQLSTATE_DATA.search(text)
            or any(w in text.lower() for w in _DATA_ERROR_WORDS)):
        m = _TARGET_TYPE.search(text.splitlines()[0] if text else "")
        target = f" converting to {m.group(1)}" if m else ""
        return (f"{name}{target}: a value in the data did not fit the query's cast, "
                f"parse or format (the value is withheld). Check the column's type and "
                f"format in the schema.")
    return redact_values(text)

# The agent's DuckDB connections (security audit item 30, SECURITY_STANDARDS
# 14.2): no file or network access, no extension installs or autoloads, and the
# configuration locked so generated SQL cannot SET it back. The guard already
# refuses file readers and settings; this is the layer under it, so a guard
# escape still reaches neither /proc/self/environ nor the network. json, icu and
# parquet are built into the wheel and stay available; the database file itself
# is opened by the connection, which external access does not govern.
#
# Applied with SET after connecting, not as connect-time config: DuckDB refuses
# a second connection to the same file in one process with a different config,
# and every connection to one file shares one database instance -- so these
# settings (global) cover every connection to it, the eval harness's reference
# backend included.
# Any setting these connections need (a future memory_limit, threads,
# timezone...) must be added HERE: once lock_down runs, nothing can change it.
AGENT_DB_SETTINGS = (
    ("autoinstall_known_extensions", "false"),
    ("autoload_known_extensions", "false"),
    ("enable_external_access", "false"),
)


def lock_down(con) -> None:
    """Apply AGENT_DB_SETTINGS and lock the configuration. Idempotent: an
    instance another connection already locked is left as it is, after
    checking it really is locked down."""
    for name, value in AGENT_DB_SETTINGS:
        current = str(con.execute(f"SELECT current_setting('{name}')").fetchone()[0]).lower()
        if current != value:
            con.execute(f"SET {name} = {value}")
    if str(con.execute("SELECT current_setting('lock_configuration')").fetchone()[0]).lower() \
            != "true":
        con.execute("SET lock_configuration = true")


def connect_agent_db(path: str, read_only: bool, seed=None):
    """Open a DuckDB connection for agent queries, locked down (lock_down).
    `seed(con)` runs first (demo tables in :memory:)."""
    con = duckdb.connect(path, read_only=read_only)
    if seed is not None:
        seed(con)
    lock_down(con)
    return con


class AnalyticsQueryTool(BaseTool):
    def __init__(self, db_path: str = ":memory:", seed_demo: bool = True,
                 row_cap: int = 1000, ledger=None, db_paths: Dict[str, str] | None = None):
        self._db_path = db_path
        self._row_cap = row_cap
        self._ledger = ledger          # optional QueryLedger for groundedness
        self._primary_error = None
        self._cursor_lock = threading.Lock()   # held only to open a cursor
        try:
            # read-only unless it is to be seeded with demo tables: the agent's
            # path never writes, and a function slipping the guard (checkpoint())
            # must not be able to either
            self._con = connect_agent_db(
                db_path, read_only=not seed_demo and db_path != ":memory:",
                seed=(lambda c: self._seed_demo_gold(c)) if seed_demo else None)
        except Exception as e:
            # The PRIMARY connection failing is what actually takes the API
            # server down at startup: app.py passes the hospital Gold here, and
            # a held lock on that file (an eval sweep, a second server) raised
            # straight out of __init__ before FastAPI ever finished booting.
            #
            # Degrade instead. An empty in-memory database means queries fail
            # with a clear "table does not exist" rather than the process
            # refusing to start, /health still answers, and the other datasets
            # -- opened read-only below -- still serve. The reason is kept so
            # the visibility endpoints can say what happened rather than
            # presenting a locked warehouse as an empty one.
            self._primary_error = f"{type(e).__name__}: {e}"
            print(f"WARNING: primary database {db_path!r} could not be opened "
                  f"({e}); starting with an empty in-memory database.")
            self._con = connect_agent_db(":memory:", read_only=False)
            seed_demo = False
        # Multi-dataset support: one tool can serve several Gold databases (e.g.
        # hospital + clinical), chosen per request via params['dataset']. The
        # default connection above stays the fallback, so existing callers that
        # pass only db_path are unaffected.
        self._cons: Dict[str, Any] = {}
        # Why a dataset is absent, when it is absent for a reason worth saying.
        # An empty entry means "never built"; a populated one means "present but
        # unusable", which is a different problem with a different fix.
        self._dataset_errors: Dict[str, str] = {}
        _default = os.path.abspath(db_path) if db_path != ":memory:" else None
        for name, path in (db_paths or {}).items():
            if not os.path.exists(path):
                continue
            # DuckDB refuses a second connection to the same file with different
            # settings, so reuse the primary connection when the path matches
            # rather than opening a conflicting read-only one.
            if _default and os.path.abspath(path) == _default:
                self._cons[name] = self._con
            else:
                # Existing on disk is not the same as being openable. DuckDB
                # refuses a connection to a file another process holds, so a
                # running eval sweep or a second server is enough to raise here
                # -- and an exception in this loop takes the WHOLE service down
                # at startup over one busy dataset, while the others were
                # perfectly serveable. A missing file is already skipped; an
                # unopenable one is the same situation and gets the same
                # treatment. /datasets then reports it unavailable, exactly as
                # it does for one that was never built.
                try:
                    self._cons[name] = connect_agent_db(path, read_only=True)
                except Exception as e:      # locked, corrupt, wrong version
                    self._dataset_errors[name] = str(e)
                    print(f"WARNING: dataset {name!r} at {path} could not be "
                          f"opened ({e}); serving without it.")
        # Per-request backend selection. DuckDB (self._con) is always available;
        # Databricks is built when its creds are present (or DATA_BACKEND=databricks).
        # Both run the SAME guard-approved safe_sql -- the guard runs FIRST either
        # way (see execute), so governance is identical. The Databricks path is
        # VERIFIED against a real workspace; needs databricks-sql-connector.
        self._databricks = None
        self._databricks_error = None
        want_dbx = os.environ.get("DATA_BACKEND", "local").lower() == "databricks"
        have_creds = all(os.environ.get(k) for k in
                         ("DATABRICKS_SERVER_HOSTNAME", "DATABRICKS_HTTP_PATH", "DATABRICKS_TOKEN"))
        if want_dbx or have_creds:
            try:
                from data_backends import DatabricksBackend
                self._databricks = DatabricksBackend()
            except BaseException as e:   # SystemExit (creds) / ImportError (driver)
                self._databricks_error = f"{type(e).__name__}: {e}"
        self._default_backend = "databricks" if (want_dbx and self._databricks) else "local"

    @property
    def name(self) -> str:
        return "analytics.query_aggregate"

    def datasets(self) -> list:
        """Dataset names this tool can serve (for the UI's dataset switcher)."""
        return sorted(self._cons)

    def dataset_errors(self) -> Dict[str, str]:
        """Datasets that exist on disk but could not be opened, and why."""
        return dict(self._dataset_errors)

    def backends(self) -> Dict[str, Any]:
        """Which data backends this tool can serve -- for visibility endpoints."""
        return {
            "default": self._default_backend,
            "available": ["local"] + (["databricks"] if self._databricks else []),
            # /health shows these unauthenticated (item 40): a file name, not a
            # server path, and the error's type, not its text (which carries
            # paths or connection details; the startup log keeps it)
            "local_db": os.path.basename(self._db_path) if self._db_path else self._db_path,
            "databricks_configured": self._databricks is not None,
            "databricks_error": _error_type(self._databricks_error),
            "local_db_error": _error_type(self._primary_error),
        }

    def execute(self, params: Dict[str, Any]) -> ToolResult:
        sql = params.get("sql")
        if not isinstance(sql, str) or not sql.strip():
            return ToolResult(success=False, output=None,
                              error="Parameter 'sql' (a SELECT query) is required.")

        # Layer 2: validate the specific query before it can run.
        decision = validate_query(sql, row_cap=self._row_cap)
        if not decision.allowed:
            return ToolResult(
                success=False,
                output={"sql": sql, "findings": decision.findings},
                error=f"DENIED by sql_guard: {decision.reason}",
            )

        # Allowed. Run the safe SQL (row cap already applied by the guard) against
        # the chosen backend -- per-request 'backend' param, else the default.
        backend = str(params.get("backend") or self._default_backend).lower()
        try:
            if backend == "databricks":
                if self._databricks is None:
                    return ToolResult(success=False, output={"safe_sql": decision.safe_sql},
                                      error=f"Databricks backend not configured: "
                                            f"{_error_type(self._databricks_error) or 'set DATABRICKS_* env vars'}")
                cols, rows = self._databricks.execute(decision.safe_sql,
                                                      max_rows=self._row_cap)
            else:
                ds = params.get("dataset")
                con = self._cons.get(ds, self._con) if ds else self._con
                # A cursor of its own per execution (backlog item 20): DuckDB
                # connections are not safe to share across threads, cursors of
                # one connection are, and a cursor of a read-only database is
                # read-only. Concurrent requests no longer queue on one lock,
                # and the timeout's interrupt reaches only this query.
                with self._cursor_lock:
                    cur = con.cursor()
                try:
                    cols, rows = run_with_timeout(cur, decision.safe_sql, query_timeout(),
                                                  max_rows=self._row_cap)
                finally:
                    cur.close()

            # Never more than the row cap leaves this tool (item 43): the fetch
            # stopped one row past it, so a longer result is marked truncated.
            truncated = len(rows) > self._row_cap
            rows = rows[: self._row_cap]

            # The PHI output check (phi_guard.py): trace every result column to
            # its sources and refuse PHI however it was renamed, and refuse
            # month/day dates and zip-shaped values. The refusal names the
            # column and the rule, never a value -- it reaches the audit log.
            phi_note = None
            if decision.phi_sql:
                try:
                    phi_guard.check_output(decision, cols, rows)
                except phi_guard.OutputRefused as refusal:
                    return ToolResult(success=False,
                                      output={"safe_sql": decision.safe_sql, "backend": backend},
                                      error=f"DENIED by sql_guard: {refusal}")
                # Safe Harbor: an age of 90 or more is shown as "90+"
                rows = phi_guard.cap_age_values(decision, cols, rows)
                if decision.phi_suppressed:
                    phi_note = (f"groups of fewer than {phi_guard.MIN_CELL} people are not "
                                f"shown (CMS small-cell rule)")
                if decision.phi_aggregate_root and not rows:
                    phi_note = (f"suppressed: fewer than {phi_guard.MIN_CELL} people match, so "
                                f"the number is not shown (CMS small-cell rule)")

            # Record real rows for groundedness verification (before redaction).
            query_id = self._ledger.record(cols, rows) if self._ledger is not None else None

            return ToolResult(
                success=True,
                output={
                    "query_id": query_id,
                    "backend": backend,
                    "dataset": params.get("dataset"),
                    "safe_sql": decision.safe_sql,
                    "row_count": len(rows),
                    "columns": cols,
                    "rows": rows,
                    **({"truncated": True, "row_cap": self._row_cap} if truncated else {}),
                    **({"phi_note": phi_note} if phi_note else {}),
                },
            )
        except QueryTimeout as e:
            return ToolResult(success=False, output={"safe_sql": decision.safe_sql, "backend": backend},
                              error=f"query execution failed: {e}")
        except Exception as e:
            return ToolResult(success=False, output={"safe_sql": decision.safe_sql, "backend": backend},
                              error=f"query execution failed: {describe_execution_error(e)}")

    # -- demo data only; delete when you point at real Gold -----------------
    def _seed_demo_gold(self, con=None) -> None:
        con = con if con is not None else self._con
        con.execute("""
            CREATE TABLE gold_utilization (
                region VARCHAR, year INTEGER,
                admits_per_1k DOUBLE, ed_per_1k DOUBLE, readmit_per_1k DOUBLE
            );
            INSERT INTO gold_utilization VALUES
                ('Bronx', 2023, 312.4, 689.1, 18.2),
                ('Manhattan', 2023, 271.8, 540.6, 15.1),
                ('Queens', 2023, 298.0, 612.3, 16.7),
                ('Westchester', 2023, 240.5, 498.2, 13.4);

            CREATE TABLE gold_cost (
                region VARCHAR, year INTEGER, service_category VARCHAR, pmpm DOUBLE
            );
            INSERT INTO gold_cost VALUES
                ('Bronx', 2023, 'inpatient', 412.10),
                ('Bronx', 2023, 'outpatient', 188.45),
                ('Manhattan', 2023, 'inpatient', 380.22),
                ('Queens', 2023, 'inpatient', 401.77);

            CREATE TABLE gold_region_profile (
                region VARCHAR, beneficiaries INTEGER
            );
            INSERT INTO gold_region_profile VALUES
                ('Bronx', 142500), ('Manhattan', 98700),
                ('Queens', 121300), ('Westchester', 76400);

            CREATE TABLE gold_anomaly (
                region VARCHAR, metric VARCHAR, anomaly_score DOUBLE
            );
            INSERT INTO gold_anomaly VALUES
                ('Bronx', 'readmit_per_1k', 3.4),
                ('Queens', 'ed_per_1k', 2.1),
                ('Manhattan', 'pmpm', 3.9);
        """)
