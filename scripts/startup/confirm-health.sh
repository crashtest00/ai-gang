#!/usr/bin/env bash
# Step 9 — confirm every service is healthy, then record completion and
# the Django admin address.
#
# The four services this flow brings up are Redis, the work-item service,
# ScrumMaster and the configured project's container. Jira, Cloudflare,
# Jenkins and the Beta VM are later phases an operator adds, and are not
# checked here because this flow never started them.
#
# Running is not the same as serving this project. The work-item service
# answers /health whatever its project list holds, so a service that came
# up before the configured project was registered looks perfectly healthy
# while every command written for that project waits unread. That is
# checked here on its own, by name.

set -euo pipefail
STARTUP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$STARTUP_DIR/lib.sh"

begin_step confirm-health
require_command docker

load_decisions

unhealthy=()

check() {
  local name="$1"; shift
  if "$@" >/dev/null 2>&1; then
    log "  $name: healthy"
    status service "$name" healthy
  else
    warn "  $name: NOT healthy"
    status service "$name" unhealthy
    unhealthy+=("$name")
  fi
}

redis_ok() { docker exec ai-gang-redis redis-cli ping 2>/dev/null | grep -q PONG; }
wis_ok() { workitem_api_healthy; }
# The work-item service's consumers create a project's consumer group as
# they start, from the project list they read then. A consumers process
# older than the project's registration has no group for it, and the
# commands a dispatched story produces sit on the stream with nothing
# reading them — no subtask, no agent, and no error. The group is the
# proof that this project is served, not merely that a container runs.
consumers_ok() { workitem_commands_consumed "$PROJECT_NAME"; }
scrummaster_ok() {
  docker exec scrummaster node -e \
    "fetch('http://localhost:9000/health').then(r => process.exit(r.ok ? 0 : 1)).catch(() => process.exit(1))"
}
# The project container is healthy when its Redis subscriber is up: that
# is the process that turns a dispatched story into an agent run, and a
# container without it is running but deaf.
project_ok() {
  docker exec "${PROJECT_NAME}-dev" pm2 jlist 2>/dev/null \
    | grep -q '"name":"subscriber"'
}

log "confirming service health"
check redis redis_ok
check work-item-service wis_ok
check scrummaster scrummaster_ok
check "$PROJECT_NAME" project_ok
check workitem-consumers consumers_ok

if (( ${#unhealthy[@]} > 0 )); then
  detail=""
  if [[ " ${unhealthy[*]} " == *" workitem-consumers "* ]]; then
    detail=" — the work-item service is not consuming '$PROJECT_NAME': there is no '$WORKITEM_COMMAND_GROUP' consumer group on $(workitem_command_stream "$PROJECT_NAME"), so commands written for '$PROJECT_NAME' would wait there unread"
  fi
  die "these services are not healthy: ${unhealthy[*]}$detail"
fi

end_step confirm-health
status complete
log "AI Gang is up. Django admin: $AIGANG_ADMIN_URL"
log "Sign in as the AIGANG_ADMIN_USER account from .env and write a story for '$PROJECT_NAME'."
