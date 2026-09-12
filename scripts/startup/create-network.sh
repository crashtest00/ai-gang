#!/usr/bin/env bash
# Step 1 — create the `ai-gang` Docker network if it does not already
# exist.
#
# Every service compose file except Redis's declares this network
# `external: true`, and expects something else to have created it. Today
# that something is whichever of services/redis/docker-compose.yml or
# scripts/init-jenkins.sh happens to run first — an ordering an operator
# has to know about. Creating it explicitly, first, removes the ordering
# from the operator's problem.

set -euo pipefail
STARTUP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$STARTUP_DIR/lib.sh"

begin_step create-network
require_command docker

if docker network inspect ai-gang >/dev/null 2>&1; then
  log "network 'ai-gang' already exists"
else
  docker network create --driver bridge ai-gang >/dev/null \
    || die "could not create the 'ai-gang' Docker network"
  log "created network 'ai-gang'"
fi

end_step create-network
