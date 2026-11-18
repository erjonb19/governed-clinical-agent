"""The real client address behind Cloudflare -> Render (owner step 9, 2026-11-18).

Chain: client -> Cloudflare -> Render (10.x) -> 127.0.0.1 -> app, with
FORWARDED_ALLOW_IPS=127.0.0.1,10.0.0.0/8. uvicorn stops at the Cloudflare edge;
request_context takes exactly one more step left, to the address Cloudflare
appended. A forged X-Forwarded-For can never choose the address.
"""

import asyncio
import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from src.runtime import request_context as rc  # noqa: E402

OWNER = "70.23.132.236"  # the owner's address from the step 9 test
EDGE = "104.22.72.47"  # a Cloudflare edge seen in that test


def test_one_step_past_a_cloudflare_edge():
    assert rc.resolve_client(EDGE, f"{OWNER}, {EDGE}") == OWNER


def test_a_forged_entry_further_left_is_never_read():
    assert rc.resolve_client(EDGE, f"1.2.3.4, {OWNER}, {EDGE}") == OWNER
    assert rc.resolve_client(EDGE, f"1.2.3.4, 5.6.7.8, {OWNER}, {EDGE}") == OWNER


def test_a_client_on_a_cloudflare_address_is_that_address_not_what_it_wrote():
    warp = "104.28.0.9"  # in 104.24.0.0/14, Cloudflare's own egress
    assert rc.resolve_client(EDGE, f"1.2.3.4, {warp}, {EDGE}") == warp


def test_a_hop_that_is_not_cloudflare_is_kept():
    assert rc.resolve_client(OWNER, f"1.2.3.4, {OWNER}") == OWNER
    assert rc.resolve_client("203.0.113.5", "1.2.3.4") == "203.0.113.5"


def test_garbage_or_a_missing_entry_keeps_the_edge():
    assert rc.resolve_client(EDGE, EDGE) == EDGE
    assert rc.resolve_client(EDGE, f"not-an-ip, {EDGE}") == EDGE
    assert rc.resolve_client(EDGE, "") == EDGE


def test_it_can_be_turned_off():
    assert rc.resolve_client(EDGE, f"{OWNER}, {EDGE}", cloudflare=False) == EDGE


def _through_the_stack(xff, peer="127.0.0.1", trusted="127.0.0.1,10.0.0.0/8"):
    """What the app sees behind uvicorn's real proxy-headers layer."""
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    seen = {}

    async def app(scope, receive, send):
        seen["client"] = scope["client"][0]
        seen["source_ip"] = rc.current()["source_ip"]
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    stack = ProxyHeadersMiddleware(rc.RequestContext(app, keys=dict), trusted_hosts=trusted)
    scope = {
        "type": "http",
        "client": (peer, 1234),
        "scheme": "http",
        "headers": [(b"x-forwarded-for", xff.encode())],
    }

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        pass

    asyncio.run(stack(scope, receive, send))
    return seen


def test_render_chain_gives_the_real_client_and_ignores_a_forgery(monkeypatch):
    monkeypatch.setattr(rc, "TRUST_OK", True)
    # what Render passes on: (forged), client, Cloudflare edge, Render's 10.x hop
    seen = _through_the_stack(f"1.2.3.4, {OWNER}, {EDGE}, 10.201.33.4")
    assert seen == {"client": OWNER, "source_ip": OWNER}
    seen = _through_the_stack(f"{OWNER}, {EDGE}, 10.201.33.4")
    assert seen == {"client": OWNER, "source_ip": OWNER}


def test_a_request_from_outside_the_trusted_proxies_ignores_the_header(monkeypatch):
    monkeypatch.setattr(rc, "TRUST_OK", True)
    seen = _through_the_stack(f"{OWNER}, {EDGE}", peer="198.51.100.7")
    assert seen["client"] == "198.51.100.7"


def test_a_bad_trust_list_warns_and_fails_closed(monkeypatch, caplog):
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "*")
    monkeypatch.setattr(rc, "TRUST_OK", True)
    with caplog.at_level(logging.ERROR, logger="governed.proxy"):
        assert rc.check_forwarded_allow_ips() is False  # no SystemExit
    assert "NOT trusted" in caplog.text
    seen = _through_the_stack(f"1.2.3.4, {OWNER}, {EDGE}", trusted="*")
    assert seen == {"client": rc.UNTRUSTED, "source_ip": rc.UNTRUSTED}


def test_the_pinned_ranges_include_the_edges_seen_on_render():
    assert rc.is_cloudflare("104.22.72.4") and rc.is_cloudflare(EDGE)
    assert not rc.is_cloudflare(OWNER) and not rc.is_cloudflare("10.201.33.4")


@pytest.mark.network
def test_the_pinned_ranges_match_cloudflare_s_published_list():
    import urllib.request

    live = set()
    for url in ("https://www.cloudflare.com/ips-v4", "https://www.cloudflare.com/ips-v6"):
        with urllib.request.urlopen(url, timeout=20) as r:  # noqa: S310
            live |= {line.strip() for line in r.read().decode().splitlines() if line.strip()}
    assert live == {str(n) for n in rc.CLOUDFLARE_RANGES}
