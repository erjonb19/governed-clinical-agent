"""The GitHub Pages wake-up page (backlog item 9, owner's decision D2a)."""

import os
import re

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
SITE = os.path.join(ROOT, "site")
SERVICE = "https://governed-clinical-agent.onrender.com"


def _read(name):
    with open(os.path.join(SITE, name), encoding="utf-8") as f:
        return f.read()


def test_it_pings_health_without_cors_and_then_opens_the_ui():
    js = _read("wake.js")
    assert f'var SERVICE = "{SERVICE}"' in js
    assert 'fetch(SERVICE + "/v1/health", { mode: "no-cors", cache: "no-store"' in js
    assert 'window.location.replace(SERVICE + "/ui")' in js
    assert "GIVE_UP_MS" in js and "AbortController" in js


def test_it_holds_and_sends_no_key():
    for name in ("index.html", "wake.js", "wake.css"):
        text = _read(name).lower()
        for word in ("x-api-key", "api_key", "apikey", "authorization", "bearer", "localstorage"):
            assert word not in text, (name, word)


def test_its_csp_allows_only_its_own_files_and_the_service():
    html = _read("index.html")
    csp = re.search(r'http-equiv="Content-Security-Policy" content="([^"]+)"', html).group(1)
    assert "default-src 'none'" in csp and "script-src 'self'" in csp
    assert f"connect-src {SERVICE}" in csp
    assert "<script>" not in html and "<style" not in html            # no inline code
    assert not re.search(r"<[^>]+\son\w+\s*=", html, re.I)           # no inline handlers


def test_the_api_does_not_block_cross_origin_reads_of_health():
    # A Cross-Origin-Resource-Policy of same-origin on /health would make the
    # page's no-cors ping fail forever, silently. Keep it off.
    with open(os.path.join(ROOT, "app.py"), encoding="utf-8") as f:
        app = f.read()
    assert "Cross-Origin-Resource-Policy" not in app
