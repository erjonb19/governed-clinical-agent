"""Audit item 49: the release workflow is read-only except for the one job that
creates the release, which runs no repository code, and no checkout keeps the
token in .git/config."""

import os

import yaml

PATH = os.path.join(os.path.dirname(__file__), "..", "..", ".github", "workflows", "release.yml")


def _wf():
    with open(PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_only_the_release_job_can_write_contents():
    wf = _wf()
    assert wf["permissions"] == {"contents": "read"}
    writers = [
        name
        for name, job in wf["jobs"].items()
        if (job.get("permissions") or {}).get("contents") == "write"
    ]
    assert writers == ["release"], writers
    steps = wf["jobs"]["release"]["steps"]
    assert not any("uses" in s for s in steps), "the write job checks nothing out"
    assert all("pip" not in s.get("run", "") and "pytest" not in s.get("run", "") for s in steps)


def test_no_checkout_persists_credentials_and_pytest_is_pinned():
    wf = _wf()
    for name, job in wf["jobs"].items():
        for step in job["steps"]:
            if str(step.get("uses", "")).startswith("actions/checkout"):
                assert (step.get("with") or {}).get("persist-credentials") is False, name
    runs = "\n".join(s.get("run", "") for j in wf["jobs"].values() for s in j["steps"])
    # pytest pinned: by name, or (item 45) by version and hash through the CI lock
    assert "pytest==" in runs or "--require-hashes -r requirements-ci.lock" in runs
