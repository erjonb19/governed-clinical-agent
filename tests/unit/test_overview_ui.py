"""web/index.html: the Overview is the landing view, and the raw controls sit
behind Advanced (PLAN v2 item 6, D8)."""

import os
import sys
import re

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

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


def test_findings_load_without_a_key_from_the_public_route_only():
    # item 9, D1a: no key -> /public/findings; /findings only with a key (an
    # unauthenticated /findings would 401 into the console and fail the live check)
    load = HTML[HTML.index("async function loadOverview(){"):HTML.index("function openDrawer")]
    assert 'k ? await fetch("/findings",{headers:authHdrs()}) : await fetch("/public/findings")' in load
    startup = HTML[HTML.index("if(!cfg.auth_required)"):HTML.index("/* ---------- nav")]
    assert "overviewNeedsKey" not in HTML
    assert startup.count("loadOverview()") == 3   # open access, no or rejected key, good key


def test_three_tabs_for_everyone_the_rest_behind_advanced():
    nav = re.search(r'<nav id="nav">(.*?)</nav>', HTML, re.S).group(1)
    buttons = re.findall(r'<button data-v="(\w+)"([^>]*)>([^<]*)', nav)
    plain = [(v, label) for v, attrs, label in buttons if "adv" not in attrs]
    assert plain == [("overview", "Answers"), ("ask", "Ask"), ("about", "About")]
    assert {v for v, attrs, _ in buttons if "adv" in attrs} == {"review", "data", "usage", "traces"}
    assert 'id="advBadge"' in HTML                 # the queue count stays visible


def test_the_first_screen_offers_one_click_questions_for_every_finding():
    import findings
    ids = set(re.findall(r'<button data-f="(\w+)">', HTML))
    assert ids == {f["id"] for f in findings.FINDINGS}
    assert 'id="coldNote"' in HTML and "Waking the service up" in HTML
