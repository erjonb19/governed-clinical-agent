"""
replay.py
=========
Recorded LLM responses for end-to-end tests (PLAN v2 7a): the `replay` provider.

With PLANNER_PROVIDER=replay the planner never calls a model. Every call -- a
plan or a critic review -- is looked up by a key over the provider-agnostic
request:

    sha256( role (plan | critic),  sha256(system prompt),  user text )

The system prompt carries the schema, and the user text carries the question
and any retry context, so a change to either is a different request. A request
with no recording FAILS LOUDLY (ReplayMiss). There is never a network fallback:
CI must not reach a provider, and a stale recording must not pass silently.

Recording is a manual, local, one-off step (tests/e2e/README.md):

    ALLOW_REPLAY=1 E2E_RECORD=1 REPLAY_SOURCE_PROVIDER=groq PLANNER_PROVIDER=replay ...

Misses are then answered by the real provider and written to the store. A
recording holds the response text (SQL or a critic verdict), the token counts
and the model name -- no keys (hard rule 4) and no result rows (the model never
sees any).
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from typing import Callable, Optional

DEFAULT_STORE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "tests", "e2e", "recordings", "llm.json")


class ReplayMiss(RuntimeError):
    """A request with no recording. Re-record (tests/e2e/README.md)."""


def request_key(role: str, system: str, user: str) -> str:
    schema = hashlib.sha256(system.encode("utf-8")).hexdigest()[:16]
    blob = json.dumps([role, schema, user], separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class ReplayStore:
    def __init__(self, path: str = DEFAULT_STORE, record: bool = False):
        self.path, self.record = path, record
        self._lock = threading.Lock()
        try:
            with open(path, encoding="utf-8") as f:
                self.entries: dict = json.load(f)
        except FileNotFoundError:
            self.entries = {}

    @classmethod
    def from_env(cls) -> "ReplayStore":
        return cls(os.environ.get("REPLAY_STORE", DEFAULT_STORE),
                   record=os.environ.get("E2E_RECORD") == "1")

    def lookup(self, role: str, system: str, user: str,
               source: Optional[Callable[[], tuple[str, int, int, str]]] = None
               ) -> tuple[str, int, int, str]:
        """(text, prompt_tokens, completion_tokens, model) for this request.

        In record mode a miss is answered by `source` (the real provider) and
        saved; otherwise a miss raises ReplayMiss.
        """
        key = request_key(role, system, user)
        hit = self.entries.get(key)
        if hit is not None:
            return hit["text"], hit["prompt_tokens"], hit["completion_tokens"], hit["model"]
        if not (self.record and source is not None):
            raise ReplayMiss(
                f"replay miss: no recording for this {role} request "
                f"({user[:80]!r}...). Re-record with E2E_RECORD=1 -- see tests/e2e/README.md")
        text, pt, ct, model = source()
        with self._lock:
            self.entries[key] = {"role": role, "user": user[:300], "text": text,
                                 "prompt_tokens": pt, "completion_tokens": ct, "model": model}
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.entries, f, indent=2, sort_keys=True)
            os.replace(tmp, self.path)
        return text, pt, ct, model
