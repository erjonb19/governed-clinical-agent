"""Backlog item 58: the UI's esc() escapes all five HTML-special characters,
including the single quote, so a value is safe in single-quoted attributes too."""

import os
import re
import shutil
import subprocess

import pytest

PAGE = os.path.join(os.path.dirname(__file__), "..", "..", "web", "index.html")


def _esc_source() -> str:
    m = re.search(r"^const esc=.*$", open(PAGE, encoding="utf-8").read(), re.M)
    assert m, "esc() not found in web/index.html"
    return m.group(0)


def test_esc_maps_the_single_quote():
    src = _esc_source()
    assert '\'":"&#39;"' in src.replace(" ", "") or '"\'":"&#39;"' in src


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_esc_escapes_every_special_character():
    js = _esc_source() + "\nprocess.stdout.write(esc(`<a href='x' title=\"y\">&</a>`));"
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout
    assert out == "&lt;a href=&#39;x&#39; title=&quot;y&quot;&gt;&amp;&lt;/a&gt;", out
