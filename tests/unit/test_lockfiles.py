"""Hash-locked installs everywhere (backlog item 45, security audit finding 16).

Every install of the app's dependencies -- Render, the Dockerfiles, CI -- uses
`pip install --require-hashes -r <lock>`, and each lock agrees with the
requirements files it was compiled from (scripts/lock.sh). A Dependabot bump of
a requirements file without a re-lock fails here, not on a deploy.
"""

import glob
import os
import re

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")

# lock -> the requirements files scripts/lock.sh compiles it from
LOCKS = {
    "requirements-api.lock": ["requirements-api.txt"],
    "requirements.lock": ["requirements.txt"],
    "requirements-ci.lock": ["requirements-api.txt", "requirements.txt", "requirements-ci.txt"],
    "requirements-e2e.lock": ["requirements-api.txt", "requirements.txt", "requirements-e2e.txt"],
}


def _read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return f.read()


def _norm(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def _pins(text):
    """{normalised name: version} of `name[extras]==version` lines."""
    out = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        m = re.match(r"([A-Za-z0-9._-]+)(\[[^\]]*\])?==([^\s;\\]+)", line)
        if m:
            out[_norm(m.group(1))] = m.group(3)
    return out


@pytest.mark.parametrize("lock", list(LOCKS))
def test_every_locked_package_is_pinned_and_hashed(lock):
    text = _read(lock)
    assert "release-age cutoff:" in text.split("\n", 4)[2], "header lost the cutoff line"
    entries = re.split(r"\n(?=[A-Za-z0-9])", text.split("\n", 3)[3])
    assert len(entries) > 10
    for entry in entries:
        head = entry.splitlines()[0]
        assert re.match(r"[A-Za-z0-9._-]+(\[[^\]]*\])?==\S+", head), head
        assert "--hash=sha256:" in entry, f"{lock}: {head} has no hash"


@pytest.mark.parametrize("lock", list(LOCKS))
def test_each_lock_agrees_with_its_requirements(lock):
    locked = _pins(_read(lock))
    for req in LOCKS[lock]:
        for name, version in _pins(_read(req)).items():
            assert locked.get(name) == version, (
                f"{req} pins {name}=={version} but {lock} has {locked.get(name)}: "
                f"run scripts/lock.sh"
            )


def test_render_and_the_dockerfiles_install_from_a_lock_with_hashes():
    assert "pip install --require-hashes -r requirements-api.lock" in _read("render.yaml")
    assert "pip install --require-hashes -r /app/requirements-api.lock" in _read("Dockerfile")
    assert "--require-hashes -r /app/requirements.lock" in _read("docker/Dockerfile.sandbox")


def test_no_workflow_installs_a_requirements_file_without_hashes():
    for wf in glob.glob(os.path.join(ROOT, ".github", "workflows", "*.yml")):
        for line in _read(wf).splitlines():
            if "pip install" in line and re.search(r"-r\s+\S*requirements[^\s]*\.txt", line):
                pytest.fail(f"{os.path.basename(wf)}: {line.strip()}")
            if "pip install" in line and ".lock" in line:
                assert "--require-hashes" in line, (wf, line)
