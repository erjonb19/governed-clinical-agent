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
* more date formats than the screen refuses: day-month-year with dashes, year
  first with slashes or dots, ISO timestamps, "7th March 1931", "7-Mar-1931";
* ages over 89 (HIPAA Safe Harbor), written as "93 years old", "93-year-old",
  "93 y/o", "93 y.o.", "93yo", "age 93" or "aged 93"; they become `[age 90+]`;
* zip codes: ZIP+4 anywhere, and five digits (or ZIP+4) after "zip", "zip
  code", "postal code" or a two-capital state code ("Bronx, NY 10467", "NY,
  10467");
* an SSN written with spaces and no label, and a seven-digit local phone number;
* any run of 7 or more digits, which covers unlabelled record and account
  numbers (it also masks an NPI, which is fine in a trace).

Text over MAX_CHARS (4,000) is withheld whole rather than scanned: a few rules
are quadratic on crafted input.

It does NOT find names, street addresses or free-form places: that needs an NLP
model. Nothing calls it on the request path yet; where it is used is listed in
docs/BACKLOG.md item 23.
"""

from __future__ import annotations

import re

from question_screen import _RULES as _SCREEN_RULES

_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
# Bounded spacing: pasted clinical text often has double spaces, and a bounded
# run stays linear where adjacent \s* pieces were quadratic.
_SP = r"\s{0,3}"
_SEP = rf"(?:\s{{1,3}}|{_SP}-{_SP})"  # " ", "  ", "-", " - "
_AGE_WORDS = (
    rf"(?:years?|yrs?)\.?(?:{_SEP}old)?"
    rf"|y{_SP}[/.-]?{_SP}o\b\.?"
    r"|y\b"
)
_OVER_89 = r"(?:9\d|1\d\d)(?:\.\d+)?"

# Longer text is not scanned (some rules are quadratic on crafted input, and
# a question is capped at 2,000 characters anyway): it is withheld whole.
MAX_CHARS = 4000
TOO_LONG = "[text withheld: too long to redact]"

# A lookbehind must have a fixed width, so the state code before a zip is
# spelled out for one to three spaces, with or without a comma.
_STATE_BEFORE = "|".join(
    rf"(?<=\b[A-Z]{{2}}{comma}{' ' * n})" for comma in ("", ",") for n in (1, 2, 3)
)

_EXTRA_RULES: list[tuple[str, re.Pattern[str]]] = [
    (
        # Formats the screen's date rule misses: day-month-year with dashes,
        # year first with slashes or dots, ISO timestamps, "7th March 1931",
        # "7-Mar-1931", "March 7 , 1931".
        "full date",
        re.compile(
            r"(?<!\d)(?:\d{1,2}-\d{1,2}-\d{4}|\d{4}[/.]\d{1,2}[/.]\d{1,2}|"
            r"\d{4}-\d{1,2}-\d{1,2}(?:T[\d:.]+Z?)?(?=T|\b)|"
            rf"\d{{1,2}}(?:st|nd|rd|th)?{_SEP}{_MONTH}{_SEP}\d{{4}}|"
            rf"{_MONTH}{_SP}\d{{1,2}}(?:st|nd|rd|th)?{_SP},?{_SP}\d{{4}})(?!\d)",
            re.I,
        ),
    ),
    (
        "age 90+",
        re.compile(
            rf"\b{_OVER_89}{_SP}-?{_SP}(?:{_AGE_WORDS})"
            rf"|\bage[sd]?(?:\s{{1,3}}of)?(?:{_SP}[:=])?{_SP}{_OVER_89}(?:{_SP}(?:{_AGE_WORDS}))?(?![\d%]|st|nd|rd|th)",
            re.I,
        ),
    ),
    (
        "zip code",
        re.compile(
            r"\b\d{5}-\d{4}\b"
            rf"|\b(?i:zip(?:{_SP}-?{_SP}code)?|postal{_SP}code)(?:{_SP}[:#])?{_SP}\d{{5}}(?:-\d{{4}})?\b"
            # a state code is two CAPITALS: "of 12000" is not a zip
            rf"|(?:{_STATE_BEFORE})\d{{5}}\b"
        ),
    ),
    (
        "social security number",
        re.compile(r"(?<!\d)\d{3}\s{1,3}\d{2}\s{1,3}\d{4}(?!\d)"),
    ),
    ("phone number", re.compile(rf"(?<![\d-])\d{{3}}{_SP}-{_SP}\d{{4}}(?![\d-])")),
    ("number", re.compile(r"(?<!\d)\d{7,}(?!\d)")),
]

# The screen's rules first (a phone or SSN is labelled as such before the
# long-number rule could take it), then the extra ones.
RULES: list[tuple[str, re.Pattern[str]]] = list(_SCREEN_RULES) + _EXTRA_RULES


def redact(text: str | None) -> tuple[str, list[str]]:
    """`text` with every identifier replaced by `[kind]`, and the kinds found.

    The kinds are in rule order, each once; no matched value is returned. Text
    over MAX_CHARS is withheld whole (fail closed)."""
    out = text or ""
    if len(out) > MAX_CHARS:
        return TOO_LONG, ["too long"]
    found: list[str] = []
    for kind, rx in RULES:
        out, n = rx.subn(f"[{kind}]", out)
        if n and kind not in found:
            found.append(kind)
    return out, found
