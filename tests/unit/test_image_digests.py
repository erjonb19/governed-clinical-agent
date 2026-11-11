"""Audit item 50: every base image and service image is pinned by digest, so a
moved tag cannot change what the build or CI runs."""

import glob
import os
import re

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
DIGEST = re.compile(r"@sha256:[0-9a-f]{64}\b")


def _image_refs():
    for path in [
        os.path.join(ROOT, "Dockerfile"),
        *glob.glob(os.path.join(ROOT, "docker", "Dockerfile*")),
    ]:
        for line in open(path, encoding="utf-8"):
            if line.strip().upper().startswith("FROM "):
                yield os.path.relpath(path, ROOT), line.strip()
    for path in [
        os.path.join(ROOT, "docker-compose.yml"),
        *glob.glob(os.path.join(ROOT, ".github", "workflows", "*.yml")),
    ]:
        for line in open(path, encoding="utf-8"):
            m = re.match(r"\s*image:\s*(\S+)", line)
            if m and ":" in m.group(1) and not m.group(1).startswith("$"):
                yield os.path.relpath(path, ROOT), line.strip()


def test_every_image_is_pinned_by_digest():
    refs = list(_image_refs())
    assert len(refs) >= 4, refs
    loose = [(p, line) for p, line in refs if not DIGEST.search(line)]
    assert not loose, loose
