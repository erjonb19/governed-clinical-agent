"""The FHIR on-push subset covers each question type (backlog item 1).

Owner's decision 2026-11-06: 10-15 cases on every push, the full bank nightly.
"Each question type" is held to: every tier, at least two cases per tier,
both comparison modes, and every FHIR Gold table read by some reference.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from eval_bank_fhir import CASES, SUBSET_IDS

TABLES = ("gold_patient", "gold_encounter", "gold_condition", "gold_observation",
          "gold_procedure", "gold_medication")


def _subset():
    return [c for c in CASES if c["id"] in SUBSET_IDS]


def test_the_subset_is_10_to_15_real_cases():
    ids = {c["id"] for c in CASES}
    assert SUBSET_IDS <= ids, SUBSET_IDS - ids
    assert 10 <= len(SUBSET_IDS) <= 15


def test_every_tier_has_at_least_two_cases():
    tiers = {c["tier"] for c in CASES}
    for t in tiers:
        assert sum(c["tier"] == t for c in _subset()) >= 2, t


def test_both_comparison_modes_are_covered():
    assert {c["mode"] for c in _subset()} == {c["mode"] for c in CASES}


def test_every_fhir_table_is_read():
    sql = " ".join(c["reference_sql"].lower() for c in _subset())
    for t in TABLES:
        assert t in sql, t
