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
"""

from __future__ import annotations

import contextvars
import hashlib
import hmac
import ipaddress
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


def check_forwarded_allow_ips() -> None:
    """Refuse to start unless every trusted proxy entry is a private or loopback
    address or network (fail closed). `*`, `0.0.0.0/0`, `::/0` or any public
    network would let callers name their own address; a name that is not an
    address is refused too."""
    raw = os.environ.get("FORWARDED_ALLOW_IPS", "")
    for part in (p.strip() for p in raw.split(",")):
        if not part:
            continue
        if not _trustable(part):
            raise SystemExit(
                f"FORWARDED_ALLOW_IPS entry {part!r} is not a private IPv4 or IPv6 "
                "(fc00::/7) address or network, or loopback: it would let callers set "
                "their own client address, or be ignored. List the proxy's own "
                "addresses (backlog item 38)"
            )


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
        ctx = {
            "request_id": request_id,
            "principal": principal(_presented_key(headers), self.keys()),
            "source_ip": client[0] if client else "unknown",
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
