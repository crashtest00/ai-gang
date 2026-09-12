#!/usr/bin/env bash
# Step 3 — build and start the work-item service, and apply its database
# migrations.
#
# Migrations are not a separate command here: that stack's compose file
# already has a one-shot `migrate` service that every other service waits
# on (`condition: service_completed_successfully`), so bringing the stack
# up runs them and nothing serves traffic until they have finished
# cleanly. Waiting for the API to answer /health is therefore also proof
# that the migrations succeeded.

set -euo pipefail
STARTUP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$STARTUP_DIR/lib.sh"

begin_step start-work-item-service
require_command docker

[[ -f "$AIGANG_ROOT/services/work-item-service/.env" ]] \
  || die "services/work-item-service/.env is missing — scripts/startup/derive-env.sh has not run"

compose_in services/work-item-service up -d --build \
  || die "the work-item service stack did not start"

for _ in $(seq 1 60); do
  if docker exec work-item-service curl -fsS http://localhost:9100/health >/dev/null 2>&1; then
    log "work-item service is answering /health (migrations applied)"
    status service work-item-service healthy
    end_step start-work-item-service
    exit 0
  fi
  sleep 2
done

status service work-item-service unhealthy
die "the work-item service did not answer /health within 120s"
