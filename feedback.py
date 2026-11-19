"""
feedback.py
===========
Thumbs up or down on an answer (backlog item 24).

One rating per answer, keyed by the answer's trace id: a later rating of the
same answer replaces the earlier one. The reason is optional free text, so it is
screened like a question (question_screen, audit item 41) before it is stored,
it never goes into a trace or a log line, and it is kept FEEDBACK_RETENTION_DAYS
(default 90, like the approval queue's free text, item 60; 0 keeps it forever).

Storage is the same as the approval queue's (state_db): Postgres when
STATE_DB_URL is set, else FEEDBACK_DB (default logs/feedback.sqlite). On Render's
free plan without Postgres the file is wiped on a redeploy, like every SQLite
store there.

`scripts/feedback_to_cases.py` turns each thumbs-down into a candidate eval case
for the owner to finish.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import state_db

FEEDBACK_DB = os.environ.get("FEEDBACK_DB", "logs/feedback.sqlite")
RATINGS = ("up", "down")
MAX_REASON_CHARS = 500


def retention_days() -> int:
    try:
        return int(os.environ.get("FEEDBACK_RETENTION_DAYS", "90"))
    except ValueError:
        return 90


class FeedbackStore:
    def __init__(self, path: Optional[str] = None):
        self.path = path or FEEDBACK_DB
        with state_db.connect(self.path) as con:
            con.execute("""
                CREATE TABLE IF NOT EXISTS answer_feedback (
                    trace_id    TEXT PRIMARY KEY,
                    rating      TEXT NOT NULL,      -- up | down
                    reason      TEXT,
                    rated_at    TEXT NOT NULL
                )
            """)
        self.retention_days = retention_days()
        self._purge_due()

    def rate(self, trace_id: str, rating: str, reason: Optional[str] = None) -> None:
        if rating not in RATINGS:
            raise ValueError(f"rating must be one of {RATINGS}")
        reason = (reason or "").strip()[:MAX_REASON_CHARS] or None
        now = datetime.now(timezone.utc).isoformat()
        self._purge_due()  # on every write: a process that never restarts still forgets
        with state_db.connect(self.path) as con:
            # the same upsert in SQLite (3.24+) and Postgres
            con.execute(
                "INSERT INTO answer_feedback (trace_id, rating, reason, rated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (trace_id) DO UPDATE SET rating = excluded.rating, "
                "reason = excluded.reason, rated_at = excluded.rated_at",
                (trace_id, rating, reason, now),
            )

    def summary(self) -> dict[str, Any]:
        """Counts only, never reasons (what /metrics shows)."""
        with state_db.connect(self.path) as con:
            rows = con.execute(
                "SELECT rating, COUNT(*) AS n FROM answer_feedback GROUP BY rating"
            ).fetchall()
        counts = {r["rating"]: int(r["n"]) for r in rows}
        up, down = counts.get("up", 0), counts.get("down", 0)
        total = up + down
        return {
            "total": total,
            "up": up,
            "down": down,
            "down_share": round(down / total, 3) if total else None,
        }

    def rated_down(self) -> list[dict[str, Any]]:
        with state_db.connect(self.path) as con:
            rows = con.execute(
                "SELECT trace_id, reason, rated_at FROM answer_feedback "
                "WHERE rating = 'down' ORDER BY rated_at"
            ).fetchall()
        return [dict(r) for r in rows]

    def _purge_due(self) -> None:
        if self.retention_days > 0:
            self.purge(self.retention_days)

    def purge(self, older_than_days: int) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat()
        with state_db.connect(self.path) as con:
            n = len(
                con.execute(
                    "SELECT trace_id FROM answer_feedback WHERE rated_at < ?", (cutoff,)
                ).fetchall()
            )
            if n:
                con.execute("DELETE FROM answer_feedback WHERE rated_at < ?", (cutoff,))
        return n
