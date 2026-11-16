"""question_screen.py -- an identifier pass on free text before it reaches a model provider.

Audit item 41: the visitor's question went to the planner's provider (Gemini by
default, a third party) exactly as typed. The data here is public CMS data and
synthetic patients, so a question never needs a real person's identifier; one
that carries one is refused before any model call, and only the KINDS found are
logged, never the text.

Rule-based, standard library only. It catches identifiers with a fixed shape
(email, phone, SSN, record numbers, patient UUIDs, full dates -- any full
date, so "encounters on 2020-03-01" is refused too; the FHIR guard refuses
date literals finer than the year anyway). It does NOT catch bare 9- or
10-digit numbers (NPIs and facility ids look the same), names or free-form addresses: that needs an NLP redactor (backlog item 23,
Presidio, a new dependency awaiting the owner's OK).
"""

from __future__ import annotations

import re

_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("email address", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")),
    (
        "social security number",
        re.compile(r"\b\d{3}-\d{2}-\d{4}\b|\bssn\s*[:#]?\s*\d{3}\s?-?\d{2}\s?-?\d{4}\b", re.I),
    ),
    (
        "phone number",
        re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?(?:\(\d{3}\)\s?|\d{3}[\s.-])\d{3}[\s.-]\d{4}(?!\d)"),
    ),
    (
        "record number",
        re.compile(
            r"\b(?:mrn|medical record(?: number)?|record (?:no\.?|number|#)|patient (?:id|number|#))"
            r"\s*[:#]?\s*[a-z0-9-]*\d[a-z0-9-]{3,}",
            re.I,
        ),
    ),
    (
        "patient identifier",
        re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I),
    ),
    (
        "full date",
        re.compile(
            r"\b(?:\d{4}-\d{1,2}-\d{1,2}|\d{1,2}/\d{1,2}/\d{2,4}|"
            r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}|"
            r"\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{4}|"
            r"\d{1,2}\.\d{1,2}\.\d{4})\b",
            re.I,
        ),
    ),
]


def identifiers_in(text: str | None) -> list[str]:
    """The kinds of personal identifier found in `text`, in rule order, no values."""
    return [kind for kind, rx in _RULES if rx.search(text or "")]


def refusal(kinds: list[str]) -> str:
    return (
        "This question looks like it contains a personal identifier ("
        + ", ".join(kinds)
        + "). Questions are sent to a third-party language model, and this service holds "
        "public and synthetic data only, so it never needs one: please remove it and ask again."
    )
