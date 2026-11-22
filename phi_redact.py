"""phi_redact.py -- remove identifiers from free text before it leaves the server.

Backlog item 23, regex-only mode (owner's decision 2026-11-22). Standard library only.
Microsoft Presidio, which would also find names, is a later option: it adds a new
dependency and a language model whose memory has to fit Render's free 512 MB.

`question_screen` REFUSES a question that carries an identifier. This module
REDACTS: every match is replaced by a bracketed label such as `[phone number]`, so
the rest of the text stays readable. It is deliberately broader than the screen,
because masking too much in a trace costs nothing, while a refusal costs the
visitor an answer:

* everything `question_screen` detects (email, SSN, phone, labelled record
  numbers, patient UUIDs, full dates);
* ages over 89 (HIPAA Safe Harbor), written as "93 years old", "93-year-old",
  "93 y/o", "age 93" or "aged 93"; they become `[age 90+]`;
* zip codes: ZIP+4 anywhere, and five digits after "zip", "zip code", "postal
  code" or a two-letter state code ("Bronx, NY 10467");
* any run of 7 or more digits, which covers unlabelled record and account
  numbers (it also masks an NPI, which is fine in a trace).

It does NOT find names, street addresses or free-form places: that needs an NLP
model. Nothing calls it on the request path yet; where it is used is listed in
docs/BACKLOG.md item 23.
"""

from __future__ import annotations

import re

from question_screen import _RULES as _SCREEN_RULES

_AGE_WORDS = r"(?:years?|yrs?)(?:[\s-]+old)?|y/?o\b"
_OVER_89 = r"(?:9\d|1[0-4]\d)"

_EXTRA_RULES: list[tuple[str, re.Pattern[str]]] = [
    (
        "age 90+",
        re.compile(
            rf"\b{_OVER_89}\s*-?\s*(?:{_AGE_WORDS})|\bage[sd]?\s*(?:of\s*)?[:=]?\s*{_OVER_89}\b",
            re.I,
        ),
    ),
    (
        "zip code",
        re.compile(
            r"\b\d{5}-\d{4}\b"
            r"|\b(?i:zip(?:\s*code)?|postal\s*code)\s*[:#]?\s*\d{5}\b"
            # a state code is two CAPITALS: "of 12000" is not a zip
            r"|(?<=\b[A-Z]{2}\s)\d{5}\b"
        ),
    ),
    ("number", re.compile(r"(?<!\d)\d{7,}(?!\d)")),
]

# The screen's rules first (a phone or SSN is labelled as such before the
# long-number rule could take it), then the extra ones.
RULES: list[tuple[str, re.Pattern[str]]] = list(_SCREEN_RULES) + _EXTRA_RULES


def redact(text: str | None) -> tuple[str, list[str]]:
    """`text` with every identifier replaced by `[kind]`, and the kinds found.

    The kinds are in rule order, each once; no matched value is returned."""
    out = text or ""
    found: list[str] = []
    for kind, rx in RULES:
        out, n = rx.subn(f"[{kind}]", out)
        if n:
            found.append(kind)
    return out, found
