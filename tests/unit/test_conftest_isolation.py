"""The suite's isolation fixtures actually run (silent failure found 2026-11-24).

`_no_answer_cache_by_default` lost its `@pytest.fixture(autouse=True)` when item
19 part 1 (#72) inserted a fixture above it, so for two weeks every test ran
with the answer cache ON, writing into the repo's logs/llm_cache.sqlite and able
to serve one test's model answer to the next. Nothing failed. This test reads
the state each autouse fixture sets up, without asking for any fixture.
"""

import os


def test_the_answer_cache_is_off_and_out_of_the_repo():
    assert os.environ.get("LLM_CACHE") == "0"
    assert "logs" + os.sep + "llm_cache" not in os.environ.get("LLM_CACHE_DB", "")


def test_tests_never_see_the_real_state_db_url():
    url = os.environ.get("STATE_DB_URL", "")
    assert url == "" or url == os.environ.get("TEST_STATE_DB_URL")
