#!/usr/bin/env bash
# Step 6 — create the shared harness volume, then build and start Memory
# without its indexer.
#
# `memory-indexer` is deliberately not started here. It begins embedding the
# platform corpus at once with a local model, which is CPU-heavy, and step 9
# builds the project container while that would still be running; on a small
# host the build starves. start-project.sh (step 9) starts the indexer after
# the project container is up. Everything else in Memory starts here, and the
# `up` names those services, so the indexer is never brought up as a
# dependency or by accident.
#
# Every project container that runs an agent mounts its own subdirectory of
# one shared named volume, `aigang-harness`, at the agent harness's
# `projects/` directory, so the sessions it writes survive a recreate;
# Memory's copier reads every project's sessions from it (Durable Agent
# Session Record REQ-01). The volume has to exist before any project is
# generated or started: scripts/init-project.sh refuses without it, and a
# project's compose file names it `external`. So this step runs after
# create-network, whose `ai-gang` network Memory's stack joins, and before
# initialize-project.
#
# `docker volume create` leaves an existing volume as it is, so re-running
# is safe. Memory's compose file names the volume `external` too, so no
# `docker compose down -v` deletes it.

set -euo pipefail
STARTUP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$STARTUP_DIR/lib.sh"

begin_step start-memory
require_command docker

[[ -f "$AIGANG_ROOT/services/memory/.env" ]] \
  || die "services/memory/.env is missing — scripts/startup/derive-env.sh has not run"

docker volume create "$AIGANG_HARNESS_VOLUME" >/dev/null \
  || die "could not create the shared harness volume '$AIGANG_HARNESS_VOLUME'"
log "shared harness volume '$AIGANG_HARNESS_VOLUME' exists"

# Every service in services/memory/docker-compose.yml except memory-indexer.
compose_in services/memory up -d --build \
  memory-postgres migrate memory-copier memory-api memory-extraction memory-extractor \
  || die "Memory did not start"

for _ in $(seq 1 60); do
  if memory_running --without-indexer && docker exec memory-api curl -fsS http://localhost:9200/health >/dev/null 2>&1; then
    log "Memory is answering /health and its four other long-running containers are running; memory-indexer is held until step 9, after the project container is up"
    status service memory healthy
    end_step start-memory
    exit 0
  fi
  sleep 2
done

status service memory unhealthy
die "Memory did not come up within 120s (memory-api answering /health, the four long-running containers other than memory-indexer running)"
