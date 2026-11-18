"""Who asked, and from where, on every audit event (backlog item 38).

An ASGI middleware gives each HTTP request a request id, the principal (which
configured key it carried, as a short fingerprint), and the client address,
and puts them in a context variable. `AuditEvent` reads that variable when it
is created, so every audit event written while serving the request -- the
API's, the runtime's, the MCP mount's, an auth failure's -- carries all three.
Context variables follow the request into the worker threads FastAPI and the
MCP tools run on.

The principal is never the key: `api:<8 hex>` / `mcp:<8 hex>` is the start of
the SHA-256 of the CONFIGURED key it matched (a fingerprint that tells keys
apart after a rotation), `invalid-key` when a key was sent that matches none
(nothing about a wrong key is recorded), `anonymous` when none was sent.

The client address is the one uvicorn reports. Behind Render's proxy that is
the proxy's address unless uvicorn is told to trust the proxy's
X-Forwarded-For: set `FORWARDED_ALLOW_IPS` to the proxy's exact addresses
(uvicorn reads it; `--proxy-headers` is uvicorn's default). A wildcard is
refused at startup (`check_forwarded_allow_ips`), and so is any entry that is not a private or loopback address: it would let any caller name
its own address and step around the per-address limits (/public/findings, the
auth-failure counter).

Behind Cloudflare (Render's onrender.com edge; found 2026-11-18, owner step 9)
the chain is client -> Cloudflare -> Render (10.x) -> 127.0.0.1 -> app, so the
first untrusted hop uvicorn finds is a Cloudflare edge, not the client. When that
hop is in Cloudflare's published ranges (pinned in CLOUDFLARE_RANGES below,
fetched 2026-11-18 from cloudflare.com/ips-v4 and /ips-v6), `resolve_client`
uses `CF-Connecting-IP` (Cloudflare overwrites it on every request through its
edge), or failing that takes EXACTLY ONE more step left in X-Forwarded-For: the
entry Cloudflare itself appended for the connecting client. Anything a client forges sits further left
and is never read; a client whose own address is a Cloudflare one (Workers,
WARP) is reported as that address, never as what it wrote. No proxy setting is
needed for this. `CLOUDFLARE_CLIENT_IP=0` turns it off.

A trust list that fails the check no longer stops the deploy (owner,
2026-11-18): it logs an error, `/health` reports `proxy_trust: invalid`, and
every request's address becomes `untrusted-proxy` -- fail closed: all callers
share one bucket in the per-address limits, so a forged header buys nothing.
"""

from __future__ import annotations

import contextvars
import hashlib
import hmac
import ipaddress
import logging
import os
import re
import uuid
from collections.abc import Callable
from typing import Any

_CURRENT: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "request_context", default=None
)

# A caller may pass its own X-Request-ID for correlation, but only a plain token:
# anything else (long, spaces, control characters) is replaced by a fresh id, so
# the header can never inject text into a log line.
_SAFE_ID = re.compile(r"[A-Za-z0-9._-]{8,64}")


def current() -> dict[str, str]:
    """The request context of the code running now ({} outside a request)."""
    return dict(_CURRENT.get() or {})


def fingerprint(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()[:8]


def principal(presented: str | None, keys: dict[str, str]) -> str:
    """Name the configured key `presented` matches (constant-time compare)."""
    if not presented:
        return "anonymous"
    for name, secret in keys.items():
        if secret and hmac.compare_digest(presented.encode(), secret.encode()):
            return f"{name}:{fingerprint(secret)}"
    return "invalid-key"


log = logging.getLogger("governed.proxy")

UNTRUSTED = "untrusted-proxy"
# The process-wide verdict on FORWARDED_ALLOW_IPS, set by check_forwarded_allow_ips.
TRUST_OK = True

# Cloudflare's published edge ranges (https://www.cloudflare.com/ips-v4 and
# /ips-v6, fetched 2026-11-18). tests/unit/test_client_ip.py compares them with
# the live lists when run with the `network` marker.
CLOUDFLARE_RANGES = tuple(
    ipaddress.ip_network(n)
    for n in (
        "173.245.48.0/20",
        "103.21.244.0/22",
        "103.22.200.0/22",
        "103.31.4.0/22",
        "141.101.64.0/18",
        "108.162.192.0/18",
        "190.93.240.0/20",
        "188.114.96.0/20",
        "197.234.240.0/22",
        "198.41.128.0/17",
        "162.158.0.0/15",
        "104.16.0.0/13",
        "104.24.0.0/14",
        "172.64.0.0/13",
        "131.0.72.0/22",
        "2400:cb00::/32",
        "2606:4700::/32",
        "2803:f800::/32",
        "2405:b500::/32",
        "2405:8100::/32",
        "2a06:98c0::/29",
        "2c0f:f248::/32",
    )
)


def _ip(s: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(s.strip())
    except ValueError:
        return None


def is_cloudflare(addr: str) -> bool:
    ip = _ip(addr)
    return ip is not None and any(ip in n for n in CLOUDFLARE_RANGES)


def _host(value: str) -> str:
    """The host of one X-Forwarded-For entry, parsed as uvicorn 0.54 parses it
    (`ip:port`, `[v6]:port`): the match must land on the exact hop uvicorn
    chose, not on a look-alike an attacker wrote further left (review, PR 150)."""
    value = value.strip()
    if value.startswith("["):
        end = value.find("]")
        if end == -1:
            return value
        rest = value[end + 1 :]
        return value[1:end] if (not rest or rest.startswith(":")) else value
    if value.count(":") == 1:
        host, port = value.rsplit(":", 1)
        return host if port.isdigit() else value
    return value


def resolve_client(
    uvicorn_host: str, forwarded_for: str, cloudflare: bool = True, cf_connecting_ip: str = ""
) -> str:
    """The caller's address. `uvicorn_host` is the first hop uvicorn did not
    trust; `forwarded_for` the raw X-Forwarded-For. Only when that hop is a
    Cloudflare edge:
      1. `CF-Connecting-IP`, which Cloudflare sets on every request through its
         edge, overwriting any a client sent (a Worker cannot set it either);
      2. failing that, the X-Forwarded-For entry just left of the hop uvicorn
         chose (the one Cloudflare appended) -- one step, never more.
    Anything else keeps uvicorn's answer."""
    if not cloudflare or not is_cloudflare(uvicorn_host):
        return uvicorn_host
    cf = _ip(cf_connecting_ip) if cf_connecting_ip else None
    if cf is not None:
        return str(cf)
    hops = [h.strip() for h in forwarded_for.split(",") if h.strip()]
    # entries right of uvicorn's hop are trusted proxies, so the rightmost entry
    # whose HOST equals uvicorn's answer is exactly the hop it chose
    for i in range(len(hops) - 1, 0, -1):
        if _host(hops[i]) == uvicorn_host:
            left = _ip(_host(hops[i - 1]))
            return str(left) if left is not None else uvicorn_host
    return uvicorn_host


def check_forwarded_allow_ips() -> bool:
    """True when every trusted proxy entry is a private or loopback address or
    network. `*`, `0.0.0.0/0`, `::/0`, any public network or a name fails. A
    failure no longer stops the process: it logs an error and every request's
    address becomes `untrusted-proxy` (fail closed, see the module docstring)."""
    global TRUST_OK
    raw = os.environ.get("FORWARDED_ALLOW_IPS", "")
    bad = [p for p in (x.strip() for x in raw.split(",")) if p and not _trustable(p)]
    TRUST_OK = not bad
    if bad:
        log.error(
            "FORWARDED_ALLOW_IPS has %d entr%s that %s not a private IPv4 or IPv6 "
            "(fc00::/7) address or network, or loopback; client addresses are NOT "
            "trusted and every request is counted as %r until it is fixed (item 38)",
            len(bad),
            "y" if len(bad) == 1 else "ies",
            "is" if len(bad) == 1 else "are",
            UNTRUSTED,
        )
    return TRUST_OK


_ULA = ipaddress.ip_network("fc00::/7")


def _trustable(entry: str) -> bool:
    """Parsed as strictly as uvicorn does (`10.0.0.1/8` is an error, not a
    quiet literal), and only IPv4 private ranges, IPv6 unique-local ranges or
    loopback: Python counts IPv4-mapped, Teredo and 6to4 IPv6 ranges as
    private, yet they hold public clients."""
    try:
        net = ipaddress.ip_network(entry, strict=True)
    except ValueError:
        return False
    if net.is_loopback:
        return True
    if isinstance(net, ipaddress.IPv4Network):
        return net.is_private
    return net.subnet_of(_ULA)


def _presented_key(headers: dict[bytes, bytes]) -> str | None:
    key = headers.get(b"x-api-key")
    if key:
        return key.decode("latin-1")
    auth = headers.get(b"authorization", b"").decode("latin-1")
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip() or None
    return None


class RequestContext:
    """Pure ASGI middleware (it wraps /mcp's streamed responses too)."""

    def __init__(self, app: Any, keys: Callable[[], dict[str, str]]) -> None:
        self.app = app
        self.keys = keys

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        given = headers.get(b"x-request-id", b"").decode("latin-1")
        request_id = given if _SAFE_ID.fullmatch(given) else uuid.uuid4().hex
        client = scope.get("client")
        if not TRUST_OK:
            host = UNTRUSTED
        elif client:
            xff = b",".join(v for k, v in (scope.get("headers") or []) if k == b"x-forwarded-for")
            host = resolve_client(
                client[0],
                xff.decode("latin-1"),
                os.environ.get("CLOUDFLARE_CLIENT_IP", "1").strip() != "0",
                headers.get(b"cf-connecting-ip", b"").decode("latin-1"),
            )
        else:
            host = "unknown"
        if client and host != client[0]:
            # in place, so every later reader (rate limits, the auth-failure
            # counter, uvicorn's access log) sees the same address
            scope["client"] = (host, client[1])
        ctx = {
            "request_id": request_id,
            "principal": principal(_presented_key(headers), self.keys()),
            "source_ip": host,
        }
        token = _CURRENT.set(ctx)

        async def send_with_id(message: dict) -> None:
            if message["type"] == "http.response.start":
                hs = [
                    (k, v) for k, v in message.get("headers") or [] if k.lower() != b"x-request-id"
                ]
                hs.append((b"x-request-id", request_id.encode()))
                message = {**message, "headers": hs}
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            _CURRENT.reset(token)
