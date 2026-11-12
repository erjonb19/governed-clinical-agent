"""Secret scanning is wired in and pinned (backlog item 47)."""

import os
import re

import yaml

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")


def _load(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as f:
        return f.read()


def test_ci_runs_gitleaks_pinned_by_version_and_checksum():
    wf = yaml.safe_load(_load(".github/workflows/tests.yml"))
    job = wf["jobs"]["secret-scan"]
    env = job["env"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", env["GITLEAKS_VERSION"])
    assert re.fullmatch(r"[0-9a-f]{64}", env["GITLEAKS_SHA256"])
    steps = "\n".join(s.get("run", "") for s in job["steps"])
    assert "sha256sum -c" in steps  # the checksum is checked before the binary runs
    assert "gitleaks git --redact" in steps  # values never reach the log
    checkout = job["steps"][0]
    assert checkout["with"]["fetch-depth"] == 0  # the whole history, not only the tip
    assert checkout["with"]["persist-credentials"] is False


def test_pre_commit_hook_is_pinned_to_a_commit():
    cfg = yaml.safe_load(_load(".pre-commit-config.yaml"))
    repos = [r for r in cfg["repos"] if r["repo"].endswith("/gitleaks/gitleaks")]
    assert len(repos) == 1
    assert re.fullmatch(r"[0-9a-f]{40}", repos[0]["rev"]), repos[0]["rev"]
    assert [h["id"] for h in repos[0]["hooks"]] == ["gitleaks"]


def test_every_ignored_finding_says_why():
    lines = _load(".gitleaksignore").splitlines()
    entries = [i for i, line in enumerate(lines) if line and not line.startswith("#")]
    assert entries, "no entries: delete the file rather than keep an empty one"
    for i in entries:
        assert re.fullmatch(r"[0-9a-f]{40}:[^:]+:[a-z0-9-]+:\d+", lines[i]), lines[i]
        # a comment explaining the finding sits somewhere above each entry
        assert any(line.startswith("#") for line in lines[:i]), lines[i]
