#!/usr/bin/env bash
# The container-side suite. Four test files:
#
#   request-artifact.test.js      the artifact helper, against real Redis
#   a2a-validate.test.js          construction-time validation on the raw
#                                 file/stdin entry point (gateway-publish.js)
#   a2a-submit.test.js            the constructor an agent invokes (a2a-submit.js)
#   ../../dispatch-snapshot.test.js  the per-dispatch commons snapshot
#
# The two a2a files run their tool as a real child process against the same
# test Redis, because that is the path a producer invokes; each writes only to
# a gateway stream name of its own and deletes it, rather than flushing a
# database it shares.
#
# The last one lives in setup/ rather than here because the module it covers
# does: setup/dispatch-snapshot.js is the container runtime's, not something an
# agent runs, so it stays out of the commons it snapshots and out of the hash it
# computes. It is run from here because this is the container-side suite, and
# adding a fifth suite to the four the build verifies would hide it.
#
# Runs every test that needs Redis against the real test Redis
# (services/core/docker-compose.test.yml,
# redis://localhost:16399 — start it first if it is not already up; never
# `down` it). Holds the same suite lock
# services/core/run_tests.sh's caller does
# (flock /tmp/v4-wis-suite.lock ./run_tests.sh -q) rather than relying on
# the caller to remember it: this file's tests write to the real
# aigang:librarian:requests/:responses stream names a live librarian
# consumer also reads, so it must never run concurrently with that suite
# against the same shared test Redis container (V4 audit Pass 2 row 35).
set -euo pipefail
cd "$(dirname "$0")"

LOCK_FILE="${V4_WIS_SUITE_LOCK:-/tmp/v4-wis-suite.lock}"
export NODE_PATH="${NODE_PATH:-$(npm root -g)}"

flock "$LOCK_FILE" node --test \
  request-artifact.test.js \
  a2a-validate.test.js \
  a2a-submit.test.js \
  ../../dispatch-snapshot.test.js
