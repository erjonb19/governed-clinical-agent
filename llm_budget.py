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

Per process and in memory: a restart (or a Render redeploy) starts a fresh
day's count. Provider-side spend caps, set in each provider's console, are
the backstop for that; they are the owner's to set.
"""

from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

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

    def per_call(rate: dict) -> float:
        return (PROMPT_TOKENS_WORST * rate["input"] + out * rate["output"]) / 1_000_000

    # every call may be served by the fallback (item 8): price it at the dearer
    worst = per_call(rate_for(getattr(planner, "provider", ""), getattr(planner, "_model", "")))
    fb = getattr(planner, "fallback_provider", None)
    if fb and fb in PROVIDERS:
        worst = max(worst, per_call(rate_for(fb, PROVIDERS[fb].get("model", ""))))
    return max_attempts * CALLS_PER_ATTEMPT * worst


class BudgetExceeded(Exception):
    def __init__(self, reason: str, retry_after: int):
        super().__init__(reason)
        self.reason, self.retry_after = reason, retry_after


class DailyBudget:
    def __init__(
        self,
        max_runs: Optional[int] = None,
        max_usd: Optional[float] = None,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self.max_runs = int(max_runs if max_runs is not None else _float_env("LLM_DAILY_RUNS", 300))
        self.max_usd = float(max_usd if max_usd is not None else _float_env("LLM_DAILY_USD", 1.0))
        self.now = now
        self._lock = threading.Lock()
        self._day = ""
        self.runs = 0
        self.usd = 0.0

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
        with self._lock:
            self._roll()
            if self.runs + 1 > self.max_runs:
                raise BudgetExceeded(
                    f"daily limit of {self.max_runs} model runs reached",
                    self._seconds_to_midnight(),
                )
            if self.usd + worst_usd > self.max_usd + 1e-12:
                raise BudgetExceeded(
                    f"daily model budget of ${self.max_usd:.2f} reached",
                    self._seconds_to_midnight(),
                )
            self.runs += 1
            self.usd += worst_usd
            return worst_usd

    def settle(self, reserved: float, actual_usd: Optional[float]) -> None:
        """Replace a reservation with what the run actually cost. A run that
        cost more than its worst-case estimate is charged in full: the count
        must never undercount. (The check before the NEXT run then sees it.)"""
        if actual_usd is None or actual_usd < 0:
            return
        with self._lock:
            self._roll()
            self.usd = max(0.0, self.usd - reserved + actual_usd)

    def status(self) -> dict:
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
