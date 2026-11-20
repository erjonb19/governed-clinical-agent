"""Item 69 (supply-chain part, audit finding 40): the small setup gaps stay closed.

* the dev extras are exact pins, and the same versions CI installs;
* pip-audit covers every top-level requirements file (the locks were already covered);
* the container scan never gets the Docker socket;
* .gitignore keeps key material and env files out of git, but not .env.example.
"""

import os
import re
import shutil
import subprocess

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")


def _read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return f.read()


def _dev_extras():
    setup = _read("setup.py")
    block = setup[setup.index('"dev": GUARD_PINS + [') : setup.index("],", setup.index('"dev"'))]
    return re.findall(r'"([A-Za-z0-9_.\-]+[^"]*)"', block.split("[", 1)[1])


def test_dev_extras_are_exact_pins():
    extras = _dev_extras()
    assert extras, "no dev extras found"
    loose = [e for e in extras if "==" not in e]
    assert not loose, f"dev extras must be pinned with ==: {loose}"


def test_dev_extras_match_what_ci_installs():
    pins = dict(e.split("==") for e in _dev_extras())
    ci = dict(
        line.split("#")[0].strip().split("==")
        for line in _read("requirements-ci.txt").splitlines()
        if "==" in line.split("#")[0]
    )
    for name in ("pytest", "pytest-cov", "pytest-mock"):
        assert pins.get(name) == ci.get(name), (
            f"{name}: setup.py {pins.get(name)} vs CI {ci.get(name)}"
        )
    tests_yml = _read(".github/workflows/tests.yml")
    for name in ("ruff", "mypy"):
        installed = re.search(rf"pip install {name}==([\w.]+)", tests_yml)
        assert installed and pins.get(name) == installed.group(1), name


def test_pip_audit_covers_every_requirements_file():
    tests_yml = _read(".github/workflows/tests.yml")
    for name in sorted(os.listdir(ROOT)):
        if name.startswith("requirements") and name.endswith((".txt", ".lock")):
            assert f"-r {name}" in tests_yml, f"pip-audit does not audit {name}"


def test_the_container_scan_has_no_docker_socket():
    container = _read(".github/workflows/container.yml")
    assert "docker.sock" not in container
    assert "--input /image.tar" in container and "aquasec/trivy@sha256:" in container


def test_gitignore_has_no_bom_and_ignores_key_material():
    raw = open(os.path.join(ROOT, ".gitignore"), "rb").read()
    assert not raw.startswith(b"\xef\xbb\xbf"), "a BOM makes git read the first pattern wrong"
    if shutil.which("git") is None or not os.path.exists(os.path.join(ROOT, ".git")):
        pytest.skip("not a git checkout")

    def ignored(path):
        return subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT).returncode == 0

    for path in (".env", ".env.local", "id.pem", "tls.key", "cert.p12", "x.tfstate", "secrets/a"):
        assert ignored(path), path
    assert not ignored(".env.example")
