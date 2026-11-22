"""Backlog item 23 (regex-only mode): every planted identifier is removed from free text.

All values below are made up (555 phone numbers, invalid SSN ranges, documentation
emails); none is a real person's.
"""

import os
import sys
import tracemalloc

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import phi_redact  # noqa: E402

PLANTED = [
    ("email address", "send it to pat.doe+x@example.org please", "pat.doe+x@example.org"),
    ("social security number", "her SSN is 900-12-3456", "900-12-3456"),
    ("social security number", "ssn: 900 12 3456", "900 12 3456"),
    ("phone number", "call (617) 555-0142 tomorrow", "555-0142"),
    ("phone number", "call +1 617.555.0142", "617.555.0142"),
    ("record number", "MRN 00482913 had a fall", "00482913"),
    ("record number", "patient id: A12-7733 readmitted", "A12-7733"),
    ("patient identifier", "patient 3f2b9c1e-0a4d-4e8f-9b7a-1c2d3e4f5a6b", "3f2b9c1e-0a4d"),
    ("full date", "admitted 2021-03-07 and discharged", "2021-03-07"),
    ("full date", "seen on 3/7/2021", "3/7/2021"),
    ("full date", "born March 7th, 1931", "March 7th, 1931"),
    ("full date", "born 7 Mar 1931", "7 Mar 1931"),
    ("age 90+", "a 93-year-old woman", "93"),
    ("age 90+", "a man, 101 years old,", "101"),
    ("age 90+", "she is 95 y/o", "95"),
    ("age 90+", "aged 92 with COPD", "92"),
    ("age 90+", "age: 97", "97"),
    ("zip code", "lives in zip 10467", "10467"),
    ("zip code", "Bronx, NY 10467 area", "10467"),
    ("zip code", "postal code 02115", "02115"),
    ("zip code", "ZIP: 02115", "02115"),
    ("zip code", "mail to 10467-1234", "10467-1234"),
    ("number", "account 123456789012 closed", "123456789012"),
]


@pytest.mark.parametrize("kind, text, value", PLANTED)
def test_every_planted_identifier_is_removed(kind, text, value):
    out, kinds = phi_redact.redact(text)
    assert value not in out, out
    assert f"[{kind}]" in out and kind in kinds


def test_several_identifiers_in_one_text_are_all_removed():
    text = (
        "Pt MRN 00482913, a 94 year old from Bronx, NY 10467, seen 2021-03-07, "
        "call 617-555-0142 or pat@example.org"
    )
    out, kinds = phi_redact.redact(text)
    for value in ("00482913", "94", "10467", "2021-03-07", "555-0142", "pat@example.org"):
        assert value not in out, (value, out)
    assert set(kinds) >= {
        "record number",
        "age 90+",
        "zip code",
        "full date",
        "phone number",
        "email address",
    }


@pytest.mark.parametrize(
    "text",
    [
        "How many hospitals in NY have a star rating of 5?",
        "Average age of patients with diabetes in 2020",
        "Top 10 hospitals by heart failure readmission rate",
        "patients aged 45 to 64 with hypertension",
        "an 89-year-old cutoff",  # 89 is not over 89
        "facility 330101",  # a six-digit CMS facility id
        "how many of 12000 patients had a visit",  # five digits, no zip context
        "",
    ],
)
def test_ordinary_questions_are_left_alone(text):
    out, kinds = phi_redact.redact(text)
    assert out == text and kinds == []


def test_none_is_empty_and_kinds_carry_no_values():
    assert phi_redact.redact(None) == ("", [])
    _, kinds = phi_redact.redact("call 617-555-0142")
    assert kinds == ["phone number"]


def test_it_covers_everything_the_screen_refuses():
    import question_screen

    for text in (
        "pat@example.org",
        "900-12-3456",
        "617-555-0142",
        "MRN 00482913",
        "3f2b9c1e-0a4d-4e8f-9b7a-1c2d3e4f5a6b",
        "2021-03-07",
    ):
        assert question_screen.identifiers_in(text)
        out, kinds = phi_redact.redact(text)
        assert kinds and out.startswith("[") and out.endswith("]"), out


def test_memory_is_small_enough_for_render():
    """Regex only: a few hundred KB at most, against Render's 512 MB."""
    tracemalloc.start()
    for _ in range(200):
        phi_redact.redact("Pt MRN 00482913, 94 y/o, NY 10467, 2021-03-07, 617-555-0142 " * 20)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert peak < 1_000_000, peak
