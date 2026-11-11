"""Audit item 48: every workflow declares top-level permissions, so no job runs
with the repository's default token scope by accident."""

import glob
import os

import yaml

WORKFLOWS = os.path.join(os.path.dirname(__file__), "..", "..", ".github", "workflows")


def test_every_workflow_sets_top_level_permissions():
    files = sorted(glob.glob(os.path.join(WORKFLOWS, "*.yml"))
                   + glob.glob(os.path.join(WORKFLOWS, "*.yaml")))
    assert files
    missing = []
    for path in files:
        with open(path, encoding="utf-8") as f:
            wf = yaml.safe_load(f)
        if "permissions" not in wf:
            missing.append(os.path.basename(path))
    assert not missing, f"no top-level permissions: {missing}"
