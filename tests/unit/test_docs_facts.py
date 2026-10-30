"""The docs state facts the code can check: case counts and the CI workflows.

These numbers went stale more than once (the README said 35 hospital cases and
481 tests long after both had moved), and a stale count in CLAUDE.md is an
instruction to run the wrong thing. So the ones that are cheap to compute are
pinned here: a PR that adds an eval case or a workflow updates the docs too.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import scheduled_runs
from eval_bank import CASES as HOSPITAL
from eval_bank_fhir import CASES as FHIR

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")


def _read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return f.read()


def test_readme_states_the_eval_bank_sizes():
    readme = _read("README.md")
    assert f"`eval_bank.py` ({len(HOSPITAL)} cases" in readme
    assert f"`eval_bank_fhir.py` ({len(FHIR)} cases" in readme
    assert f"**{len(HOSPITAL) + len(FHIR)} questions across two datasets**" in readme
    assert f"the {len(HOSPITAL)}-case ground-truth suite" in readme


def test_claude_md_states_the_eval_bank_sizes():
    claude = _read("CLAUDE.md")
    assert f"{len(HOSPITAL)}-case hospital eval" in claude
    assert f"{len(FHIR)}-case FHIR eval" in claude


def test_readme_names_every_workflow():
    readme = _read("README.md")
    workflows = sorted(f for f in os.listdir(os.path.join(ROOT, ".github", "workflows"))
                       if f.endswith((".yml", ".yaml")))
    assert workflows
    for wf in workflows:
        assert f"`{wf}`" in readme, f"README's CI section does not mention {wf}"


def test_docs_name_every_standing_question():
    claude = _read("CLAUDE.md")
    for s in scheduled_runs.load_schedules()["schedules"]:
        assert f"`{s['case']}`" in claude
