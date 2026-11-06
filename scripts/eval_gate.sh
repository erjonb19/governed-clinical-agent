#!/usr/bin/env bash
# scripts/eval_gate.sh -- run the eval harness as a CI gate.
#
#   scripts/eval_gate.sh <warn|retry-then-fail> [eval_harness.py args...]
#
# eval_harness.py exits 0 (pass), 1 (accuracy below the gate: a real
# regression) or 2 (inconclusive: the provider was unavailable for most runs,
# or a case was never answered). This turns that into the build's verdict:
#
#   exit 1 -> red, always.
#   exit 2, mode "warn"            -> green with a warning. Used on push: a
#                                     provider outage must not block a commit.
#   exit 2, mode "retry-then-fail" -> wait EVAL_RETRY_WAIT_SEC (default 300),
#                                     run once more; still 2 -> red, titled
#                                     "Provider unavailable". Used nightly
#                                     (owner's decision 2026-11-06).
#
# EVAL_ATTEMPT_TIMEOUT_MIN bounds each attempt (default: none). An attempt
# that runs over is treated like exit 2 (inconclusive), so it is retried or
# reported red here. Without it GitHub's job timeout ends the run as
# "cancelled" -- grey, not red -- which is how two nightlies in Nov 2026 ended.
#
# EVAL_CMD overrides the command (tests use a stub); default python -u eval_harness.py.
set -u

mode="${1:-}"
shift || true
if [ "$mode" != "warn" ] && [ "$mode" != "retry-then-fail" ]; then
  echo "usage: $0 <warn|retry-then-fail> [eval_harness.py args...]" >&2
  exit 64
fi
cmd="${EVAL_CMD:-python -u eval_harness.py}"
wait_sec="${EVAL_RETRY_WAIT_SEC:-300}"
limit_min="${EVAL_ATTEMPT_TIMEOUT_MIN:-}"

run() {
  local rc
  if [ -n "$limit_min" ]; then
    # shellcheck disable=SC2086  # EVAL_CMD is a command line, split on purpose
    timeout --signal=TERM --kill-after=30 "${limit_min}m" $cmd "$@"
    rc=$?
    if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then
      echo "::warning title=Eval attempt timed out::An attempt ran past ${limit_min} minutes and was stopped; counted as inconclusive."
      rc=2
    fi
    return "$rc"
  fi
  # shellcheck disable=SC2086
  $cmd "$@"
}

run "$@"
code=$?

if [ "$code" -eq 2 ] && [ "$mode" = "retry-then-fail" ]; then
  echo "::warning title=Provider unavailable (attempt 1 of 2)::Eval inconclusive; retrying once in ${wait_sec}s."
  sleep "$wait_sec"
  run "$@"
  code=$?
  if [ "$code" -eq 2 ]; then
    echo "::error title=Provider unavailable::Eval inconclusive twice: the LLM provider was unavailable (402/429/5xx), cases went unanswered, or attempts ran past their time limit. Not an accuracy regression, but the nightly run cannot pass without a result."
    exit 2
  fi
fi

if [ "$code" -eq 2 ]; then
  echo "::warning title=Provider unavailable::Eval inconclusive -- the LLM provider was unavailable for most runs (402/429/5xx). This is NOT an accuracy regression."
  exit 0
fi
if [ "$code" -eq 1 ]; then
  echo "::error title=Eval below threshold::Accuracy over completed runs fell below the gate. Real regression -- see the uploaded report."
fi
exit "$code"
