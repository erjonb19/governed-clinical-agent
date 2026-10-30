"""web/index.html: the Overview is the landing view, and the raw controls sit
behind Advanced (PLAN v2 item 6, D8)."""

import os
import re

HTML = open(os.path.join(os.path.dirname(__file__), "..", "..", "web", "index.html"),
            encoding="utf-8").read()


def test_overview_is_the_landing_view():
    nav = re.search(r'<nav id="nav">(.*?)</nav>', HTML, re.S).group(1)
    first = re.search(r'<button data-v="(\w+)"([^>]*)>', nav)
    assert first.group(1) == "overview" and 'class="on"' in first.group(2)
    assert '<section class="view on" id="v-overview">' in HTML
    assert '<section class="view" id="v-ask">' in HTML
    assert len(re.findall(r'<section class="view on"', HTML)) == 1


def test_overview_has_definitions_and_limits():
    for term in ("Star rating", "Medicare spending per patient", "30-day readmission rate",
                 "ED median time", "Psychiatric ED time", "Left before being seen", "Data as of"):
        assert f"<dt>{term}" in HTML, term
    assert "How this works, and its limits" in HTML


def test_the_raw_controls_are_advanced_only():
    for ident in ("segMode", "segDataset", "segBackend", "gearBtn"):
        tag = re.search(rf'<[^>]*id="{ident}"[^>]*>', HTML).group(0)
        assert "adv" in tag, ident
    assert 'class="adv"><a href="#" id="toTrace"' in HTML
    assert "body:not(.advanced) .adv{display:none!important}" in HTML


def test_advanced_state_survives_unavailable_storage():
    # every localStorage touch for the toggle is inside try/catch
    block = HTML[HTML.index('const ADV="ci_advanced"'):HTML.index("function findingCard")]
    for m in re.finditer(r"localStorage\.\w+Item\(ADV", block):
        before = block[:m.start()]
        assert before.rfind("try{") > before.rfind("}catch"), block[m.start() - 60:m.end()]


def test_findings_are_not_fetched_without_a_key():
    # an unauthenticated /findings would 401 into the console (the live check fails on it)
    startup = HTML[HTML.index("if(!cfg.auth_required)"):HTML.index("/* ---------- nav")]
    assert "overviewNeedsKey()" in startup
    i = startup.index("loadOverview()")
    assert "refreshBadge(); loadOverview()" in startup[i - 20:i + 20]
