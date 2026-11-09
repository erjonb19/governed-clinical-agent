"""
versioning.py
=============
Which version of the agent produced an answer (backlog item 4): the prompt
version, the model, the provider and the git commit, on every /query response,
its trace and its audit line.

- prompt_version: "p-" + 12 hex of sha256 over the exact system prompt and
  critic prompt the planner was given for the dataset (schema notes included).
  Derived from the text, so it cannot be forgotten when a prompt changes: any
  edit to a prompt or a schema note is a new version.
- git_commit: RENDER_GIT_COMMIT (Render sets it), GITHUB_SHA (CI), GIT_COMMIT
  (anything else), then `git rev-parse HEAD` in the repo, else "unknown".
- model, provider: the provider that served the call (with the fallback of
  item 8 that can differ from PLANNER_PROVIDER) and its model.
"""

from __future__ import annotations

import functools
import hashlib
import os
import subprocess
from typing import Any, Optional

ROOT = os.path.dirname(os.path.abspath(__file__))


@functools.lru_cache(maxsize=1)
def git_commit() -> str:
    for var in ("RENDER_GIT_COMMIT", "GITHUB_SHA", "GIT_COMMIT"):
        v = (os.environ.get(var) or "").strip()
        if v:
            return v[:40]
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
                             text=True, timeout=5)
        sha = out.stdout.strip()
        if out.returncode == 0 and len(sha) == 40:
            return sha
    except Exception:
        pass
    return "unknown"


@functools.lru_cache(maxsize=16)
def prompt_version(schema_doc: Optional[str] = None) -> str:
    from nl_to_sql_planner import SCHEMA_DOC, build_critic_prompt, build_system_prompt
    doc = schema_doc or SCHEMA_DOC
    blob = build_system_prompt(doc) + "\n\0critic\0\n" + build_critic_prompt(doc)
    return "p-" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def answer_version(metrics: Any, schema_doc: Optional[str] = None,
                   planner: Any = None) -> dict:
    """The four fields for one answer. `metrics` is a CallMetrics, its as_dict(),
    or None (no call completed, e.g. the provider was down); then the configured
    planner's provider and model are given, since that is who was asked."""
    if metrics is not None and not isinstance(metrics, dict):
        metrics = metrics.as_dict() if hasattr(metrics, "as_dict") else vars(metrics)
    m = metrics or {}
    return {
        "prompt_version": prompt_version(schema_doc),
        "model": m.get("model") or getattr(planner, "_model", None),
        "provider": m.get("provider") or getattr(planner, "provider", None),
        "git_commit": git_commit(),
    }
