"""Throwaway: proves a failing test turns a push to a non-main branch red
(backlog item 1). This branch is deleted after the run."""


def test_deliberately_fails():
    assert False, "deliberate failure for the CI demonstration"
