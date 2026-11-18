"""Backlog item 62: secret redaction knows this repo's own key formats.

The sample keys are built at run time, so no key-shaped string sits in the
source for the secret scanner to flag; none of them is a real key.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from src.utils.redaction import REDACTED, redact_data, redact_text  # noqa: E402

A = "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"  # 36 filler characters, not a key
SAMPLES = {
    "gemini": "AIza" + "Sy" + A[:33],
    "groq": "gsk" + "_" + A + "XyZw",
    "xai": "xai" + "-" + A,
    "anthropic": "sk" + "-ant-api03-" + A + "_" + A[:10],
    "openai_project": "sk" + "-proj-" + A + "-" + A[:8],
    "langfuse_secret": "sk" + "-lf-" + "1234abcd-12ab-34cd-56ef-1234567890ab",
    "langfuse_public": "pk" + "-lf-" + "1234abcd-12ab-34cd-56ef-1234567890ab",
    "databricks": "dapi" + "0123456789abcdef" * 2,
}


@pytest.mark.parametrize("kind", list(SAMPLES))
def test_each_provider_key_is_masked_in_text(kind):
    key = SAMPLES[kind]
    out = redact_text(f"provider error: invalid key {key} (status 401)")
    assert key not in out and REDACTED in out, (kind, out)


@pytest.mark.parametrize("kind", list(SAMPLES))
def test_each_provider_key_is_masked_in_a_value_under_an_innocent_key(kind):
    key = SAMPLES[kind]
    out = redact_data({"detail": f"rejected: {key}"})
    assert key not in str(out), (kind, out)


@pytest.mark.parametrize(
    "text",
    [
        "SELECT count(*) FROM gold_patient",
        "the task-ask list",  # 'sk-' inside a word
        "xai is a vendor; gsk is a prefix",
        "AIza alone is not a key",
    ],
)
def test_ordinary_text_is_untouched(text):
    assert redact_text(text) == text
