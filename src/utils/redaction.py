"""Sensitive data redaction helpers."""

from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED]"
TRUNCATED_SUFFIX = "... [TRUNCATED]"
MAX_TEXT_LENGTH = 4096

_SENSITIVE_KEY_TERMS = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "auth",
    "authorization",
    "cookie",
    "session",
    "credential",
    "private_key",
    "access_key",
    "refresh_token",
)

_SENSITIVE_PATH_RE = re.compile(
    r"(?i)(?:^|[\\/])(?:\.env(?:\.[\w.-]+)?|id_rsa|id_dsa|id_ed25519|authorized_keys|[^\\/]+\.(?:pem|p12|pfx|key))(?:$|[\\/])"
)
_AUTH_HEADER_RE = re.compile(r"(?im)\b(authorization)\s*:\s*[^\r\n]+")
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")
_PRIVATE_KEY_BLOCK_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
)
_GITHUB_TOKEN_RE = re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")
_OPENAI_TOKEN_RE = re.compile(r"\bsk-[A-Za-z0-9]{16,}\b")
# Backlog item 62: the key formats this repo itself uses (and close relatives).
_PROVIDER_KEY_RES = (
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"),  # Google / Gemini
    re.compile(r"\bgsk_[A-Za-z0-9]{20,}"),  # Groq
    re.compile(r"\bxai-[A-Za-z0-9]{20,}"),  # xAI
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"),  # Anthropic (sk-ant-api03-...)
    re.compile(r"\bsk-(?:proj|svcacct|admin)-[A-Za-z0-9_-]{20,}"),  # OpenAI project/service keys
    re.compile(r"\b[ps]k-lf-[A-Za-z0-9-]{8,}"),  # Langfuse (item 12's hosted traces)
    re.compile(r"\bdapi[0-9a-f]{32}(?:-\d+)?\b"),  # Databricks personal access token
)


def _is_sensitive_key(key: str) -> bool:
    key_lower = key.lower()
    return any(term in key_lower for term in _SENSITIVE_KEY_TERMS)


def _truncate_text(text: str) -> str:
    if len(text) <= MAX_TEXT_LENGTH:
        return text
    return f"{text[:MAX_TEXT_LENGTH]}{TRUNCATED_SUFFIX}"


def redact_text(text: str) -> str:
    """
    Redact sensitive tokens/patterns from free-form text.

    Redaction is best-effort and intentionally conservative:
    - known auth headers/tokens are masked
    - private key blocks are replaced wholesale
    - common secret file indicators in paths are masked
    - long text is truncated to avoid excessive data exposure
    """
    if not isinstance(text, str):
        return _truncate_text(str(text))

    redacted = text
    redacted = _PRIVATE_KEY_BLOCK_RE.sub(REDACTED, redacted)
    redacted = _AUTH_HEADER_RE.sub(r"\1: [REDACTED]", redacted)
    redacted = _BEARER_RE.sub("Bearer [REDACTED]", redacted)
    redacted = _GITHUB_TOKEN_RE.sub(REDACTED, redacted)
    for pattern in _PROVIDER_KEY_RES:  # before the shorter sk- rule
        redacted = pattern.sub(REDACTED, redacted)
    redacted = _OPENAI_TOKEN_RE.sub(REDACTED, redacted)
    redacted = _SENSITIVE_PATH_RE.sub("[REDACTED_PATH]", redacted)
    return _truncate_text(redacted)


def redact_data(data: Any) -> Any:
    """
    Recursively redact sensitive data while preserving container shape.

    Supported types:
    - dict/list/tuple (recursive traversal)
    - str (pattern redaction)
    - bytes/bytearray (length-only placeholder)
    - scalar primitives (pass-through)
    - other objects (stringified + redacted)
    """
    if isinstance(data, dict):
        redacted: dict[Any, Any] = {}
        for key, value in data.items():
            key_name = str(key)
            if _is_sensitive_key(key_name):
                redacted[key] = REDACTED
                continue
            redacted[key] = redact_data(value)
        return redacted

    if isinstance(data, list):
        return [redact_data(item) for item in data]

    if isinstance(data, tuple):
        return tuple(redact_data(item) for item in data)

    if isinstance(data, str):
        return redact_text(data)

    if isinstance(data, (bytes, bytearray)):
        return f"[BINARY_DATA_REDACTED:{len(data)} bytes]"

    if isinstance(data, (int, float, bool)) or data is None:
        return data

    return redact_text(str(data))
