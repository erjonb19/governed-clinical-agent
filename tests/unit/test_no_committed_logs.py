"""Item 65: nothing under logs/ is committed. logs/ is gitignored because the
service writes audit and trace files there that can hold questions, client
addresses and key fingerprints; ten old demo audit logs had been committed
before the ignore rule and stayed tracked."""

import os
import shutil
import subprocess

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")


def test_no_file_under_logs_is_tracked():
    if shutil.which("git") is None or not os.path.exists(os.path.join(ROOT, ".git")):
        pytest.skip("not a git checkout")
    tracked = subprocess.run(
        ["git", "ls-files", "--", "logs"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.split()
    assert not tracked, f"files under logs/ are committed: {tracked}"
