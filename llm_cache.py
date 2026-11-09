"""
llm_cache.py
============
A cache of model ANSWERS (the SQL a plan call wrote, the verdict a critic
call gave), so a repeated question skips the LLM (backlog item 6).

What it never caches: result rows, guard decisions or anything downstream. A
hit returns the model's text and the request goes on exactly as if the model
had just written it: the guard validates it, the query runs on today's data,
the PHI output check runs. So a data refresh cannot serve old rows, and a guard
change cannot let old SQL through -- both happen after the cache.

The key is the request itself, like the replay store (replay.request_key: the
role, a hash of the full system prompt with its schema notes, and the user
text with any retry context), plus the provider and model asked, plus the data
vintage (medallion/REFRESH.json) and a hash of the guard's files. The last two
are belt and braces: they make a refresh or a guard edit start a cold cache.

SQLite at LLM_CACHE_DB (default logs/llm_cache.sqlite). On Render's free plan
the disk is not lasting, so the cache starts cold after a restart or redeploy;
that only costs a model call. LLM_CACHE=0 turns it off; LLM_CACHE_TTL_SEC
(default 86400) bounds an entry's age. The eval harness turns it off, so a run
always asks the model.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import sqlite3
import threading
import time
from typing import Any, Optional

ROOT = os.path.dirname(os.path.abspath(__file__))
GUARD_FILES = ("sql_guard.py", "phi_guard.py", "phi_columns.yaml")


def enabled() -> bool:
    return os.environ.get("LLM_CACHE", "1").strip().lower() not in ("0", "false", "no", "off")


@functools.lru_cache(maxsize=1)
def guard_version() -> str:
    h = hashlib.sha256()
    for name in GUARD_FILES:
        try:
            with open(os.path.join(ROOT, name), "rb") as f:
                h.update(name.encode() + b"\0" + f.read())
        except OSError:
            h.update(name.encode() + b"\0missing")
    return h.hexdigest()[:12]


def data_vintage() -> str:
    """The hospital Gold's refresh date; read per call (a monthly refresh can
    land without a restart). "unknown" when the file is missing."""
    try:
        with open(os.path.join(ROOT, "medallion", "REFRESH.json"), encoding="utf-8") as f:
            return str(json.load(f).get("last_refreshed") or "unknown")
    except Exception:
        return "unknown"


# A retry prompt carries the previous attempt's failure (agent_graph._plan,
# approval_graph._plan). Never cached: a repeated failure would hand back the
# same failed SQL instead of letting the model correct itself.
RETRY_MARKERS = ("Your previous SQL attempt was REJECTED", "Your previous SQL was REJECTED")


def cacheable(role: str, user: str, text: str) -> bool:
    """Only first attempts, only usable answers."""
    if not text or not text.strip() or any(m in user for m in RETRY_MARKERS):
        return False
    if role == "critic":
        import critic

        return critic.parse_verdict(text)["verdict"] != "unavailable"
    return True


def key(role: str, system: str, user: str, provider: str, model: str) -> str:
    import replay

    blob = json.dumps(
        [replay.request_key(role, system, user), provider, model, data_vintage(), guard_version()],
        separators=(",", ":"),
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class LLMCache:
    def __init__(self, path: Optional[str] = None, ttl_sec: Optional[float] = None):
        self.path = path or os.environ.get("LLM_CACHE_DB", os.path.join("logs", "llm_cache.sqlite"))
        self.ttl = float(
            ttl_sec if ttl_sec is not None else os.environ.get("LLM_CACHE_TTL_SEC", "86400")
        )
        self.hits = 0
        self.misses = 0
        self.evictions = 0
        self._lock = threading.Lock()
        self._db: Optional[sqlite3.Connection] = None

    def _conn(self) -> sqlite3.Connection:
        if self._db is None:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            db = sqlite3.connect(self.path, check_same_thread=False)
            db.execute(
                "CREATE TABLE IF NOT EXISTS answers (key TEXT PRIMARY KEY, role TEXT,"
                " text TEXT, provider TEXT, model TEXT, created REAL)"
            )
            self._db = db
        return self._db

    def get(self, k: str) -> Optional[dict[str, Any]]:
        """{text, provider, model} or None. Never raises: a broken cache is a miss."""
        try:
            with self._lock:
                row = (
                    self._conn()
                    .execute(
                        "SELECT text, provider, model, created FROM answers WHERE key = ?", (k,)
                    )
                    .fetchone()
                )
                if row is None or (self.ttl > 0 and time.time() - row[3] > self.ttl):
                    self.misses += 1
                    return None
                self.hits += 1
                return {"text": row[0], "provider": row[1], "model": row[2]}
        except Exception:
            with self._lock:
                self.misses += 1
            return None

    def put(self, k: str, role: str, text: str, provider: str, model: str) -> None:
        try:
            with self._lock:
                self._conn().execute(
                    "INSERT OR REPLACE INTO answers VALUES (?, ?, ?, ?, ?, ?)",
                    (k, role, text, provider, model, time.time()),
                )
                self._conn().commit()
        except Exception:
            pass  # best effort: a cache must never fail a request

    def evict(self, k: Optional[str]) -> None:
        """Forget one answer: its SQL was refused or failed, so the next ask
        should go back to the model rather than replay the failure."""
        if not k:
            return
        try:
            with self._lock:
                self._conn().execute("DELETE FROM answers WHERE key = ?", (k,))
                self._conn().commit()
                self.evictions += 1
        except Exception:
            pass

    def stats(self) -> dict[str, Any]:
        total = self.hits + self.misses
        try:
            with self._lock:
                entries = self._conn().execute("SELECT count(*) FROM answers").fetchone()[0]
        except Exception:
            entries = None
        return {
            "enabled": enabled(),
            "hits": self.hits,
            "misses": self.misses,
            "evictions": self.evictions,
            "hit_rate": round(self.hits / total, 4) if total else None,
            "entries": entries,
            "ttl_sec": self.ttl,
            "note": "since this process started; the store resets on a Render restart",
        }


_cache: Optional[LLMCache] = None
_cache_lock = threading.Lock()


def get_cache() -> LLMCache:
    global _cache
    with _cache_lock:
        if _cache is None:
            _cache = LLMCache()
        return _cache


def set_cache(cache: Optional[LLMCache]) -> None:
    """For tests: a fresh cache (or None to rebuild from the environment)."""
    global _cache
    with _cache_lock:
        _cache = cache
