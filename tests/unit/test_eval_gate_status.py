"""Unit tests for the eval harness's gate verdict.

Two distinct mistakes have to stay impossible here:

  1. Counting runs that never reached the provider as wrong answers. That is
     what turned eval_single_hospital_20260827T181558Z into a recorded 71.4%
     "failure" when all ten of its failing runs were HTTP 429s and not one was
     a wrong answer.
  2. Ruling confidently on a sample too thin to support a verdict. Scoring over
     completed runs alone would let a sweep that mostly 429'd report a healthy
     accuracy computed from the handful that got through.

The verdict must therefore be "pass"/"fail" only when enough runs actually
executed, and "provider_unavailable" otherwise.
"""

import pytest

from eval_harness import _gate_status, MIN_COMPLETED_FRACTION, THRESHOLD


# --- clean runs: the gate rules normally ----------------------------------

def test_all_runs_correct_passes():
    status, acc, completed = _gate_status(105, 105, 0)
    assert (status, acc, completed) == ("pass", 1.0, 105)


def test_genuine_regression_fails():
    # no infra trouble at all -- the agent really did get 2 in 3 right
    status, acc, completed = _gate_status(70, 105, 0)
    assert status == "fail"
    assert acc == pytest.approx(70 / 105)
    assert completed == 105


def test_exactly_at_threshold_passes():
    status, _, _ = _gate_status(int(THRESHOLD * 100), 100, 0)
    assert status == "pass"


# --- provider errors must not read as wrong answers -----------------------

def test_provider_errors_are_excluded_from_the_denominator():
    # 5 of 100 runs never reached the model; the other 95 were all correct.
    # Scored over all runs this is 95% -- scored correctly it is a clean 100%.
    status, acc, completed = _gate_status(95, 100, 5)
    assert status == "pass"
    assert acc == 1.0
    assert completed == 95


def test_regression_still_detected_despite_some_provider_errors():
    # 10 unreachable runs must not launder a real regression in the other 90
    status, acc, completed = _gate_status(45, 100, 10)
    assert status == "fail"
    assert acc == pytest.approx(0.5)
    assert completed == 90


# --- thin samples are inconclusive, never a verdict -----------------------

def test_total_outage_is_inconclusive():
    status, _, completed = _gate_status(2, 84, 82)
    assert status == "provider_unavailable"
    assert completed == 2


def test_the_2026_08_27_shape_is_inconclusive_not_a_failure():
    # The recorded failure: 25 correct, 10 provider errors, 35 runs. It is not
    # a regression (nothing was answered wrongly) and it is not a pass either
    # (10 of 35 cases never ran) -- it is a run that must be repeated.
    status, acc, completed = _gate_status(25, 35, 10)
    assert status == "provider_unavailable"
    assert acc == 1.0            # every run that executed was correct
    assert completed == 25


def test_just_under_half_provider_errors_is_inconclusive():
    # The gap this floor closes: at 49% provider errors the old rule declared a
    # confident pass computed from barely half the suite.
    status, _, _ = _gate_status(18, 35, 17)
    assert status == "provider_unavailable"


def test_zero_runs_is_inconclusive_not_a_divide_by_zero():
    status, acc, completed = _gate_status(0, 0, 0)
    assert status == "provider_unavailable"
    assert acc == 0.0
    assert completed == 0


# --- the floor itself ------------------------------------------------------

def test_boundary_is_inclusive_at_the_floor():
    total = 100
    at_floor = int(MIN_COMPLETED_FRACTION * total)          # 80 completed
    assert _gate_status(at_floor, total, total - at_floor)[0] == "pass"
    assert _gate_status(at_floor - 1, total, total - at_floor + 1)[0] == \
        "provider_unavailable"
