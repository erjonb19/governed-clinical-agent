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
    assert not re.search(r"<script(?![^>]*\bsrc=)", html, re.I)  # no inline script
    assert not re.search(r"<style", html, re.I)  # no inline style block
    assert not re.search(r"\sstyle\s*=", html, re.I)  # no style attributes
    assert not re.search(r"<[^>]*[\s/]on[a-z]+\s*=", html, re.I)  # no inline handlers


def test_the_api_does_not_block_cross_origin_reads_of_health(monkeypatch, tmp_path):
    # A Cross-Origin-Resource-Policy of same-origin or same-site on /health would
    # make the page's no-cors ping fail forever, silently. Check the real response.
    import importlib
    import sys

    from fastapi.testclient import TestClient

    sys.path.insert(0, ROOT)
    for k, v in {
        "API_KEY": "k",
        "TRACE_DB": tmp_path / "t.sqlite",
        "CHECKPOINT_DB": tmp_path / "c.sqlite",
        "APPROVALS_DB": tmp_path / "a.sqlite",
        "AUDIT_LOG_DIR": tmp_path / "audit",
    }.items():
        monkeypatch.setenv(k, str(v))
    import app as app_module

    importlib.reload(app_module)
    with TestClient(app_module.app) as c:
        for path in ("/v1/health", "/health"):
            corp = c.get(path).headers.get("cross-origin-resource-policy", "").lower()
            assert corp not in ("same-origin", "same-site"), (path, corp)
