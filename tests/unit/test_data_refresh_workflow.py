"""Audit item 44: main is protected, so the monthly data refresh proposes its
Gold as a PR instead of pushing to main, and a failed push or PR fails the
job (it used to fall through to "no changes to commit" and pass)."""

import os

import yaml

PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", ".github", "workflows", "data-refresh.yml"
)


def _run_lines():
    with open(PATH, encoding="utf-8") as f:
        wf = yaml.safe_load(f)
    return [
        line.strip()
        for job in wf["jobs"].values()
        for step in job["steps"]
        for line in step.get("run", "").splitlines()
    ]


def test_the_refresh_opens_a_pr_and_never_pushes_to_main():
    lines = _run_lines()
    assert any(line.startswith("gh pr create --base main") for line in lines)
    pushes = [line for line in lines if line.startswith("git push")]
    assert pushes == ['git push origin "$branch"'], pushes


def test_a_failed_push_is_not_swallowed():
    lines = _run_lines()
    assert not any("git push" in line and "||" in line for line in lines), lines
