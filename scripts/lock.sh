#!/usr/bin/env bash
# Regenerate the hash-pinned lockfiles (backlog item 45, security audit finding 16).
#
#   pip install uv==0.11.32 && scripts/lock.sh
#
# Every install (Render, the Dockerfiles, CI) uses `pip install --require-hashes -r
# <lock>`: the whole dependency tree is pinned to exact versions AND the hashes of
# their files, so a re-uploaded or tampered release fails the install. Run this
# after any change to a requirements*.txt (a Dependabot bump included) and commit
# the .lock files with it; tests/unit/test_lockfiles.py fails while they disagree.
#
# Release-age cooldown: nothing in the tree may be newer than 7 days before the
# run (uv's --exclude-newer). The packages the requirements files pin by name are
# exempt -- each was chosen and reviewed, and Dependabot proposes them after its own
# 7-day cooldown -- and so are the two that move in lockstep with one of them
# (pydantic-core with pydantic, psycopg-binary with psycopg).
set -euo pipefail
cd "$(dirname "$0")/.."

CUTOFF="${LOCK_CUTOFF:-$(date -u -d '7 days ago' +%Y-%m-%dT00:00:00Z)}"
PINNED_UNTIL="$(date -u -d 'tomorrow' +%Y-%m-%dT00:00:00Z)"
LOCKSTEP="pydantic-core psycopg-binary"
names=$(cat requirements-api.txt requirements.txt requirements-ci.txt requirements-e2e.txt \
          | sed 's/#.*//' | grep -oE '^[A-Za-z0-9._-]+' | tr 'A-Z_' 'a-z-' | sort -u)
args=(--universal --python-version 3.11 --generate-hashes
      --exclude-newer "$CUTOFF")
for n in $names $LOCKSTEP; do
  args+=(--exclude-newer-package "$n=$PINNED_UNTIL")
done

lock() {
  local out=$1; shift
  echo "locking $out (cutoff $CUTOFF)"
  uv pip compile --quiet "${args[@]}" --custom-compile-command "scripts/lock.sh" "$@" -o "$out"
  # record the cutoff in the header (tests/unit/test_lockfiles.py reads it)
  sed -i "2a #    release-age cutoff: $CUTOFF (pinned packages exempt; see scripts/lock.sh)" "$out"
}

lock requirements-api.lock requirements-api.txt                                   # Render, Docker image, evals
lock requirements.lock     requirements.txt                                       # the sandbox image
lock requirements-ci.lock  requirements-api.txt requirements.txt requirements-ci.txt  # tests, Gold builds
lock requirements-e2e.lock requirements-api.txt requirements.txt requirements-e2e.txt # browser E2E, live check
