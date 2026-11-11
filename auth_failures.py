"""Failed API-key attempts: counted, audited and throttled per client (backlog item 35).

Every refused key (missing or wrong) on the API and on MCP over HTTP is
recorded here:

* AUDITED -- an `auth_failure` line on the `governed.auth` logger and, when the
  API's audit logger is attached, in the audit JSONL: client address, path,
  how many failures that client has in the window. Never the key, not even a
  prefix: a near-miss key is still a secret.
* COUNTED -- per client address, over `AUTH_FAILURE_WINDOW_SEC` (default 300).
* THROTTLED -- past `AUTH_FAILURES_PER_WINDOW` (default 10) failures in the
  window, that client's further WRONG keys get 429 + Retry-After instead of
  401.

A valid key is never refused here, by default. On Render the server sees the
platform proxy's address, not the caller's, until uvicorn trusts the forwarded
header (backlog item 38), so every caller shares one address: refusing valid
keys from a throttled address would let one bad client lock everyone out.
`AUTH_FAILURE_LOCKOUT=1` refuses every request from a throttled address, the
stricter policy; turn it on only where the client address is real.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Callable, Optional

log = logging.getLogger("governed.auth")


def _int_env(name: str, default: int, minimum: int) -> int:
    try:
        return max(minimum, int(os.environ.get(name, default)))
    except ValueError:
        return default


class AuthFailures:
    """Per-client failure counts over a fixed window. Thread-safe; bounded."""

    MAX_CLIENTS = 10_000

    def __init__(
        self,
        per_window: Optional[int] = None,
        window_sec: Optional[int] = None,
        lockout: Optional[bool] = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.per_window = (
            per_window if per_window is not None else _int_env("AUTH_FAILURES_PER_WINDOW", 10, 1)
        )
        self.window_sec = (
            window_sec if window_sec is not None else _int_env("AUTH_FAILURE_WINDOW_SEC", 300, 1)
        )
        if lockout is None:
            lockout = os.environ.get("AUTH_FAILURE_LOCKOUT", "").lower() in {"1", "true", "yes"}
        self.lockout = lockout
        self.clock = clock
        self._lock = threading.Lock()
        self._counts: dict[str, tuple[float, int]] = {}
        self.audit: Optional[Any] = None  # an AuditLogger, attached by the host app
        self.total = 0

    def _current(self, client: str, now: float) -> tuple[float, int]:
        start, n = self._counts.get(client, (now, 0))
        if now - start >= self.window_sec:
            return now, 0
        return start, n

    def throttled(self, client: str) -> bool:
        """True when `client` is over the limit in the current window."""
        with self._lock:
            return self._current(client, self.clock())[1] >= self.per_window

    def retry_after(self, client: str) -> int:
        with self._lock:
            now = self.clock()
            start, _ = self._current(client, now)
            return max(1, int(self.window_sec - (now - start)) + 1)

    def record(self, client: str, path: str, surface: str) -> int:
        """Count one refused key and audit it. Returns the client's count."""
        with self._lock:
            now = self.clock()
            start, n = self._current(client, now)
            n += 1
            self._counts[client] = (start, n)
            self.total += 1
            if len(self._counts) > self.MAX_CLIENTS:  # forget the oldest half
                for k in sorted(self._counts, key=lambda k: self._counts[k][0])[
                    : self.MAX_CLIENTS // 2
                ]:
                    del self._counts[k]
        throttled = n > self.per_window
        log.warning(
            "auth_failure surface=%s client=%s path=%s failures_in_window=%d%s",
            surface,
            client,
            path,
            n,
            " throttled" if throttled else "",
        )
        audit = self.audit
        if audit is not None:
            try:
                audit.log_auth_failure(
                    client=client,
                    path=path,
                    surface=surface,
                    failures_in_window=n,
                    throttled=throttled,
                )
            except Exception:  # noqa: BLE001 -- auditing must never break a refusal
                log.exception("auth_failure audit write failed")
        return n

    def reset(self) -> None:
        with self._lock:
            self._counts.clear()
            self.total = 0


FAILURES = AuthFailures()
