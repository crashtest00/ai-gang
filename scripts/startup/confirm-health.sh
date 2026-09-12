#!/usr/bin/env bash
# Step 9 — confirm every service is healthy, then record completion and
# the Django admin address.
#
# The four services this flow brings up are Redis, the work-item service,
# ScrumMaster and the configured project's container. Jira, Cloudflare,
# Jenkins and the Beta VM are later phases an operator adds, and are not
# checked here because this flow never started them.

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
wis_ok() { docker exec work-item-service curl -fsS http://localhost:9100/health; }
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

if (( ${#unhealthy[@]} > 0 )); then
  die "these services are not healthy: ${unhealthy[*]}"
fi

end_step confirm-health
status complete
log "AI Gang is up. Django admin: $AIGANG_ADMIN_URL"
log "Sign in as the AIGANG_ADMIN_USER account from .env and write a story for '$PROJECT_NAME'."
