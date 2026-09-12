#!/usr/bin/env bash
# Step 8 — build and start the project container.
#
# The container's own entrypoint starts the Redis subscriber under pm2 on
# every start, so there is no post-start command for anyone to run.
#
# ScrumMaster is restarted at the end. It loads
# services/scrummaster/config/projects.json once, at startup, and creates
# that project's streams and consumer groups from it; the project it needs
# to know about was registered in step 6, after ScrumMaster came up. Until
# it re-reads the file, a story written for this project would land
# durably in Redis and never be consumed — the one failure mode with no
# error anywhere. Restarting is how it re-reads.

set -euo pipefail
STARTUP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$STARTUP_DIR/lib.sh"

begin_step start-project
require_command docker

load_decisions

PROJECT_DIR="projects/$PROJECT_NAME"
[[ -d "$AIGANG_ROOT/$PROJECT_DIR" ]] || die "project directory $AIGANG_ROOT/$PROJECT_DIR does not exist"
[[ -f "$AIGANG_ROOT/$PROJECT_DIR/Dockerfile" ]] || die "$PROJECT_DIR/Dockerfile is missing — run the Dockerfile installation step first"

compose_in "$PROJECT_DIR" up -d --build || die "the project container did not start"

CONTAINER="${PROJECT_NAME}-dev"
for _ in $(seq 1 45); do
  if [[ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)" == "true" ]]; then
    log "project container $CONTAINER is running"
    break
  fi
  sleep 2
done

[[ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)" == "true" ]] \
  || { status service "$PROJECT_NAME" unhealthy; die "project container $CONTAINER is not running"; }

log "reloading ScrumMaster's project registry so it consumes '$PROJECT_NAME'"
compose_in services/scrummaster restart scrummaster || die "could not restart ScrumMaster"

end_step start-project
