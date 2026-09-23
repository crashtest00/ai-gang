#!/usr/bin/env bash
# Runs request-artifact.test.js against the real test Redis
# (services/work-item-service/docker-compose.test.yml,
# redis://localhost:16399 — start it first if it is not already up; never
# `down` it). Holds the same suite lock
# services/work-item-service/run_tests.sh's caller does
# (flock /tmp/v4-wis-suite.lock ./run_tests.sh -q) rather than relying on
# the caller to remember it: this file's tests write to the real
# aigang:librarian:requests/:responses stream names a live librarian
# consumer also reads, so it must never run concurrently with that suite
# against the same shared test Redis container (V4 audit Pass 2 row 35).
set -euo pipefail
cd "$(dirname "$0")"

LOCK_FILE="${V4_WIS_SUITE_LOCK:-/tmp/v4-wis-suite.lock}"
export NODE_PATH="${NODE_PATH:-$(npm root -g)}"

flock "$LOCK_FILE" node --test request-artifact.test.js
