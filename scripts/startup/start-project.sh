#!/usr/bin/env bash
# Step 8 — build and start the project container, then reload the
# services that read the project list at startup.
#
# The container's own entrypoint starts the Redis subscriber under pm2 on
# every start, so there is no post-start command for anyone to run.
#
# The reload at the end is the rest of this step. Two services read
# services/scrummaster/config/projects.json once, when they start, and
# both were started before step 6 registered the configured project in
# it:
#
#   - ScrumMaster creates each configured project's streams and consumer
#     groups from that file at startup;
#
#   - the work-item service does the same from the same file — its
#     consumers process creates each project's command and webhook
#     consumer groups as it starts, and its API process caches the
#     project list the first time it is asked for it, which step 3's own
#     health check already did.
#
# Until each of them re-reads the file, a story written for this project
# is dispatched and then goes nowhere: its follow-up commands land
# durably on the project's command stream with no consumer group to read
# them, no subtask is ever persisted, and no agent runs — the one failure
# mode with no error anywhere. Restarting is how they re-read.
#
# The outbox relay is deliberately not restarted: it takes each row's
# project from the row itself and never reads the project list.

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

log "reloading the work-item service's project registry so it consumes '$PROJECT_NAME'"
compose_in services/work-item-service restart api consumers \
  || die "could not restart the work-item service's API and consumers"

wait_until 45 workitem_api_healthy \
  || { status service work-item-service unhealthy; die "the work-item service did not answer /health again after its project registry was reloaded"; }

wait_until 30 workitem_commands_consumed "$PROJECT_NAME" \
  || die "the work-item service is not consuming '$PROJECT_NAME' after the reload — no '$WORKITEM_COMMAND_GROUP' consumer group on $(workitem_command_stream "$PROJECT_NAME")"

log "the work-item service is consuming commands for '$PROJECT_NAME'"

end_step start-project
