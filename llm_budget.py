"""A daily budget for the runs that call the model (backlog item 37).

Before item 37 the only limit on model use was a per-key rate (20/minute),
in memory, per process: a valid key could spend it around the clock. Now
every run that calls the model (/query, /propose) first RESERVES its worst
case against two daily caps, the same pattern as `scheduled_runs.py`:

* `LLM_DAILY_RUNS` (default 300) -- runs per UTC day;
* `LLM_DAILY_USD` (default 1.00) -- estimated USD per UTC day, priced with the
  planner's own rates (`nl_to_sql_planner.rate_for`; Gemini's free tier is
  billed $0, so this is an estimate, which is what makes it a safe cap).

The worst case is every attempt at full prompt and full output, for the plan
and the critic call. A run whose cost is reported back is then charged what
it actually cost; one that raises, or reports nothing, keeps its worst case.
A run starts only if its worst case fits under both caps; the worst case
uses the dearer of the primary and fallback rates, and a run that somehow
cost more is charged in full, so the count never undercounts. Past either
cap the caller gets 429 with a Retry-After that runs to the next UTC midnight.

Where the count lives (item 19, part 2, 2026-11-24): with STATE_DB_URL set
(Postgres), in the `llm_budget_day` table -- one row per UTC day, charged by
one conditional UPDATE, so a restart or a Render redeploy no longer starts a
fresh day, and two processes cannot both slip under a cap. If that database
cannot be reached, the run is REFUSED (fail closed: a run that cannot be
counted does not happen). Without STATE_DB_URL the count is per process and
in memory, as before: a restart starts a fresh day. Provider-side spend caps,
set in each provider's console, stay the backstop; they are the owner's to
set.
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

_log = logging.getLogger("llm_budget")

PROMPT_TOKENS_WORST = 4000  # the largest prompt the planner sends (scheduled_runs.py)
DEFAULT_MAX_OUTPUT_TOKENS = 800
CALLS_PER_ATTEMPT = 2  # plan + critic


def _float_env(name: str, default: float) -> float:
    try:
        v = float(os.environ.get(name, default))
    except ValueError:
        return default
    return v if v >= 0 else default


def worst_case_usd(planner: Any, max_attempts: int) -> float:
    """The most one run can cost: every attempt, plan and critic, at full size."""
    if planner is None or getattr(planner, "provider", "") == "replay":
        return 0.0  # no planner, or recorded answers (CI, load test): no model is called
    from nl_to_sql_planner import PROVIDERS, rate_for

    out = getattr(planner, "_max_tokens", DEFAULT_MAX_OUTPUT_TOKENS) or DEFAULT_MAX_OUTPUT_TOKENS

    def per_call(rate: dict, max_out: int) -> float:
        return (PROMPT_TOKENS_WORST * rate["input"] + max_out * rate["output"]) / 1_000_000

    # every call may be served by the fallback (item 8): price it at the dearer,
    # each with its own output cap (Anthropic's is 4000, the others' 800)
    worst = per_call(
        rate_for(getattr(planner, "provider", ""), getattr(planner, "_model", "")), out
    )
    fb = getattr(planner, "fallback_provider", None)
    if fb and fb in PROVIDERS:
        fb_out = PROVIDERS[fb].get("max_tokens", DEFAULT_MAX_OUTPUT_TOKENS)
        worst = max(worst, per_call(rate_for(fb, PROVIDERS[fb].get("model", "")), fb_out))
    return max_attempts * CALLS_PER_ATTEMPT * worst


class Reservation(float):
    """What reserve() charged, and the UTC day it charged: settle() corrects
    that day's row, so a run that straddles midnight cannot discount the next
    day's reservations."""

    day: str = ""


class BudgetExceeded(Exception):
    def __init__(self, reason: str, retry_after: int, unavailable: bool = False):
        super().__init__(reason)
        self.reason, self.retry_after = reason, retry_after
        self.unavailable = unavailable  # the store could not be reached (503, not 429)


BUDGET_DB = os.environ.get("LLM_BUDGET_DB", "logs/llm_budget.sqlite")
_EPS = 1e-12


class _Store:
    """The day's count in the state database (state_db: Postgres, or a SQLite
    file when a test names one). Every call is one short transaction."""

    _DDL_LOCK = threading.Lock()

    def __init__(self, sqlite_path: str):
        self.path = sqlite_path
        self._ready = False

    def _create(self) -> None:
        """The table, once per store. In its own transaction, serialised in
        this process and retried once: two processes running Postgres's
        CREATE TABLE IF NOT EXISTS at the same moment can still collide."""
        import state_db

        with self._DDL_LOCK:
            if self._ready:
                return
            for attempt in (1, 2):
                try:
                    with state_db.connect(self.path) as con:
                        real = "DOUBLE PRECISION" if con.dialect == "postgres" else "REAL"
                        con.execute(
                            "CREATE TABLE IF NOT EXISTS llm_budget_day ("
                            f"day TEXT PRIMARY KEY, runs INTEGER NOT NULL, usd {real} NOT NULL)"
                        )
                    break
                except Exception:
                    if attempt == 2:
                        raise
            self._ready = True

    def _ensure(self, con: Any) -> None:
        if not self._ready:
            self._create()

    def _retry(self, fn: Callable[..., Any], *args: Any) -> Any:
        """Once more after re-creating the table (it can be dropped under a
        long-lived process, as the test suite does). A first attempt whose
        commit outcome is unknown (the connection dropped during COMMIT) may
        have landed, so a retried charge can count twice: that only ever
        OVERcounts, the safe direction."""
        try:
            return fn(*args)
        except Exception:
            self._ready = False
            return fn(*args)

    def charge(self, day: str, worst: float, max_runs: int, max_usd: float) -> Optional[str]:
        """Charge one run: None on success, else which cap refused it."""
        result: Optional[str] = self._retry(self._charge, day, worst, max_runs, max_usd)
        return result

    def adjust(self, day: str, delta: float) -> None:
        self._retry(self._adjust, day, delta)

    def read(self, day: str) -> tuple[int, float]:
        result: tuple[int, float] = self._retry(self._read, day)
        return result

    def _charge(self, day: str, worst: float, max_runs: int, max_usd: float) -> Optional[str]:
        import state_db

        with state_db.connect(self.path) as con:
            self._ensure(con)
            con.execute(
                "INSERT INTO llm_budget_day (day, runs, usd) VALUES (?, 0, 0) "
                "ON CONFLICT (day) DO NOTHING",
                (day,),
            )
            # one statement checks both caps and charges: concurrent runs are
            # serialised on the row, and the losing one sees the new totals
            row = con.execute(
                "UPDATE llm_budget_day SET runs = runs + 1, usd = usd + ? "
                "WHERE day = ? AND runs + 1 <= ? AND usd + ? <= ? RETURNING runs",
                (worst, day, max_runs, worst, max_usd + _EPS),
            ).fetchone()
            if row is not None:
                return None
            cur = con.execute("SELECT runs FROM llm_budget_day WHERE day = ?", (day,)).fetchone()
        return "runs" if cur is None or int(cur["runs"]) + 1 > max_runs else "usd"

    def _adjust(self, day: str, delta: float) -> None:
        import state_db

        with state_db.connect(self.path) as con:
            self._ensure(con)
            con.execute(
                "UPDATE llm_budget_day SET usd = CASE WHEN usd + ? < 0 THEN 0 ELSE usd + ? END "
                "WHERE day = ?",
                (delta, delta, day),
            )

    def _read(self, day: str) -> tuple[int, float]:
        import state_db

        with state_db.connect(self.path) as con:
            self._ensure(con)
            row = con.execute(
                "SELECT runs, usd FROM llm_budget_day WHERE day = ?", (day,)
            ).fetchone()
        return (int(row["runs"]), float(row["usd"])) if row else (0, 0.0)


class DailyBudget:
    def __init__(
        self,
        max_runs: Optional[int] = None,
        max_usd: Optional[float] = None,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        store_path: Optional[str] = None,
    ):
        self.max_runs = int(max_runs if max_runs is not None else _float_env("LLM_DAILY_RUNS", 300))
        self.max_usd = float(max_usd if max_usd is not None else _float_env("LLM_DAILY_USD", 1.0))
        self.now = now
        self._lock = threading.Lock()
        self._day = ""
        self.runs = 0
        self.usd = 0.0
        # a SQLite path given here persists the count (tests); otherwise only
        # STATE_DB_URL does, checked on every call so the setting is live
        self._file_store = _Store(store_path) if store_path else None
        self._pg_store: Optional[_Store] = None

    def _store(self) -> Optional[_Store]:
        if self._file_store is not None:
            return self._file_store
        import state_db

        if not state_db.is_postgres():
            return None
        if self._pg_store is None:
            self._pg_store = _Store(BUDGET_DB)
        return self._pg_store

    def _today(self) -> str:
        return self.now().strftime("%Y-%m-%d")

    def _roll(self) -> None:
        day = self.now().strftime("%Y-%m-%d")
        if day != self._day:
            self._day, self.runs, self.usd = day, 0, 0.0

    def _seconds_to_midnight(self) -> int:
        n = self.now()
        midnight = (n + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return max(1, int((midnight - n).total_seconds()))

    def reserve(self, worst_usd: float) -> float:
        """Charge one run at its worst case, or raise BudgetExceeded."""
        store = self._store()
        if store is not None:
            try:
                day = self._today()
                refused = store.charge(day, worst_usd, self.max_runs, self.max_usd)
            except Exception:
                # fail closed: a run the budget cannot count does not happen
                raise BudgetExceeded(
                    "the model budget could not be checked", 60, unavailable=True
                ) from None
            if refused == "runs":
                raise BudgetExceeded(
                    f"daily limit of {self.max_runs} model runs reached",
                    self._seconds_to_midnight(),
                )
            if refused == "usd":
                raise BudgetExceeded(
                    f"daily model budget of ${self.max_usd:.2f} reached",
                    self._seconds_to_midnight(),
                )
            r = Reservation(worst_usd)
            r.day = day
            return r
        with self._lock:
            self._roll()
            if self.runs + 1 > self.max_runs:
                raise BudgetExceeded(
                    f"daily limit of {self.max_runs} model runs reached",
                    self._seconds_to_midnight(),
                )
            if self.usd + worst_usd > self.max_usd + _EPS:
                raise BudgetExceeded(
                    f"daily model budget of ${self.max_usd:.2f} reached",
                    self._seconds_to_midnight(),
                )
            self.runs += 1
            self.usd += worst_usd
            r = Reservation(worst_usd)
            r.day = self._day
            return r

    def settle(self, reserved: float, actual_usd: Optional[float]) -> None:
        """Replace a reservation with what the run actually cost. A run that
        cost more than its worst-case estimate is charged in full: the count
        must never undercount. (The check before the NEXT run then sees it.)"""
        if actual_usd is None or actual_usd < 0:
            return
        day = getattr(reserved, "day", "") or self._today()
        delta = actual_usd - reserved
        store = self._store()
        if store is not None:
            for _ in range(2 if delta > 0 else 1):  # an overrun is worth a retry
                try:
                    store.adjust(day, delta)
                    return
                except Exception as e:  # noqa: BLE001
                    err = e
            # a refund lost keeps the worst case charged (an overcount); an
            # overrun lost undercounts by the excess -- say so
            _log.warning("llm budget: settle failed (%s), delta %.6f", type(err).__name__, delta)
            return
        with self._lock:
            self._roll()
            if day != self._day:
                return  # the run's day is over: its count is gone with it
            self.usd = max(0.0, self.usd + delta)

    def status(self) -> dict:
        store = self._store()
        if store is not None:
            day = self._today()
            try:
                runs, usd = store.read(day)
            except Exception:  # noqa: BLE001
                runs, usd = -1, -1.0  # unknown: shown as such, never as zero
            return {
                "day": day,
                "runs": runs,
                "max_runs": self.max_runs,
                "est_usd": round(usd, 6),
                "max_usd": self.max_usd,
            }
        with self._lock:
            self._roll()
            return {
                "day": self._day,
                "runs": self.runs,
                "max_runs": self.max_runs,
                "est_usd": round(self.usd, 6),
                "max_usd": self.max_usd,
            }


BUDGET = DailyBudget()
