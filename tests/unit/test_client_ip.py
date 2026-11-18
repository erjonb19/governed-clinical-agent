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


def test_cf_connecting_ip_is_preferred_behind_cloudflare():
    assert rc.resolve_client(EDGE, f"1.2.3.4, {EDGE}", cf_connecting_ip=OWNER) == OWNER
    # a bogus value falls back to the one-step rule
    assert rc.resolve_client(EDGE, f"{OWNER}, {EDGE}", cf_connecting_ip="evil") == OWNER


def test_cf_connecting_ip_is_ignored_when_the_hop_is_not_cloudflare():
    assert rc.resolve_client(OWNER, OWNER, cf_connecting_ip="1.2.3.4") == OWNER


def test_an_edge_written_with_a_port_cannot_walk_into_forged_entries():
    # review of PR 150: a bare look-alike further left must not be matched
    xff = f"9.9.9.9, {EDGE}, {OWNER}, {EDGE}:443"
    assert rc.resolve_client(EDGE, xff) == OWNER
    v6 = "2606:4700::6812:1"
    assert rc.resolve_client(v6, f"9.9.9.9, {v6}, {OWNER}, [{v6}]:443") == OWNER


def test_an_ipv6_edge_and_client():
    v6edge, v6client = "2606:4700::6812:1", "2001:db8::42"
    assert rc.resolve_client(v6edge, f"{v6client}, {v6edge}") == v6client


def test_it_can_be_turned_off():
    assert rc.resolve_client(EDGE, f"{OWNER}, {EDGE}", cloudflare=False) == EDGE


def _through_the_stack(xff, peer="127.0.0.1", trusted="127.0.0.1,10.0.0.0/8", extra=()):
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
        "headers": [(b"x-forwarded-for", x.strip().encode()) for x in xff.split("|")] + list(extra),
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


def test_several_header_lines_and_cf_connecting_ip_through_the_stack(monkeypatch):
    monkeypatch.setattr(rc, "TRUST_OK", True)
    # two X-Forwarded-For lines, joined as one list by both uvicorn and the app
    seen = _through_the_stack(f"1.2.3.4, {OWNER} | {EDGE}, 10.201.33.4")
    assert seen["client"] == OWNER
    seen = _through_the_stack(
        f"1.2.3.4, {EDGE}, 10.201.33.4", extra=[(b"cf-connecting-ip", OWNER.encode())]
    )
    assert seen["client"] == OWNER


def test_turned_off_through_the_stack(monkeypatch):
    monkeypatch.setattr(rc, "TRUST_OK", True)
    monkeypatch.setenv("CLOUDFLARE_CLIENT_IP", "0")
    assert _through_the_stack(f"{OWNER}, {EDGE}, 10.201.33.4")["client"] == EDGE


def test_two_cf_connecting_ip_lines_are_not_believed(monkeypatch):
    monkeypatch.setattr(rc, "TRUST_OK", True)
    seen = _through_the_stack(
        f"{OWNER}, {EDGE}, 10.201.33.4",
        extra=[(b"cf-connecting-ip", b"9.9.9.9"), (b"cf-connecting-ip", OWNER.encode())],
    )
    assert seen["client"] == OWNER  # the XFF one-step rule, not either header line


def test_odd_ports_parse_like_uvicorn():
    assert rc._host("1.2.3.4:443") == "1.2.3.4"
    assert rc._host("1.2.3.4:+5") == "1.2.3.4"  # int("+5") works, as in uvicorn
    assert rc._host("1.2.3.4:x") == "1.2.3.4:x"
