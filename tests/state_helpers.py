"""What the checkpointer stored, from either backend (item 19).

Tests that prove result rows or planted values never reach a checkpoint read
the stored values. With STATE_DB_URL set (the Postgres CI job) the checkpoints
are in Postgres, not in the SQLite file -- reading only the file would pass
without checking anything.
"""

import os
import sqlite3

CHECKPOINT_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")


def _as_bytes(v):
    if isinstance(v, (bytes, bytearray, memoryview)):
        return bytes(v)
    return str(v).encode("utf-8", "replace")


def checkpoint_values(sqlite_path):
    """Every non-null stored value, as bytes."""
    url = os.environ.get("STATE_DB_URL", "")
    out = []
    if url.startswith(("postgres://", "postgresql://")):
        import psycopg

        with psycopg.connect(url) as con:
            for t in CHECKPOINT_TABLES:
                for row in con.execute(f"SELECT * FROM {t}"):
                    out += [_as_bytes(v) for v in row if v is not None]
        return out
    con = sqlite3.connect(str(sqlite_path))
    try:
        tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        for t in tables:
            for row in con.execute(f"SELECT * FROM {t}"):
                out += [_as_bytes(v) for v in row if v is not None]
    finally:
        con.close()
    return out
