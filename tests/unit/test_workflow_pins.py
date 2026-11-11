"""Security audit item 31 (SECURITY_STANDARDS 16.5): every GitHub Action is
pinned to a full commit SHA, never a movable tag (the tj-actions pattern), with
the version it corresponds to in a comment that Dependabot keeps current."""

import glob
import os
import re

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
USES = re.compile(r"^\s*(?:-\s*)?uses:\s*(\S+)(.*)$")


def _uses():
    for path in sorted(glob.glob(os.path.join(ROOT, ".github", "workflows", "*.yml"))):
        with open(path, encoding="utf-8") as f:
            for n, line in enumerate(f, 1):
                m = USES.match(line)
                if m:
                    yield os.path.basename(path), n, m.group(1), m.group(2)


def test_there_are_actions_to_check():
    assert len(list(_uses())) > 20


def test_every_action_is_pinned_to_a_full_commit_sha_with_its_version():
    bad = []
    for path, n, ref, rest in _uses():
        if ref.startswith("./") or ref.startswith("docker://"):
            continue
        if not re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", ref):
            bad.append(f"{path}:{n} {ref}")
        elif not re.search(r"#\s*v\d+\.\d+\.\d+", rest):
            bad.append(f"{path}:{n} {ref} (no '# vX.Y.Z' comment)")
    assert not bad, "\n".join(bad)
