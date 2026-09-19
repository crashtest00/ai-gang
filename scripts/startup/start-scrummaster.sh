#!/usr/bin/env bash
# Step 5 — build and start ScrumMaster, the work router.

set -euo pipefail
STARTUP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$STARTUP_DIR/lib.sh"

begin_step start-scrummaster
require_command docker

[[ -f "$AIGANG_ROOT/services/scrummaster/.env" ]] \
  || die "services/scrummaster/.env is missing — scripts/startup/derive-env.sh has not run"

compose_in services/scrummaster up -d --build || die "ScrumMaster did not start"

for _ in $(seq 1 45); do
  if docker exec scrummaster node -e "fetch('http://localhost:9000/health').then(r => process.exit(r.ok ? 0 : 1)).catch(() => process.exit(1))" >/dev/null 2>&1; then
    log "ScrumMaster is answering /health"
    status service scrummaster healthy
    end_step start-scrummaster
    exit 0
  fi
  sleep 2
done

status service scrummaster unhealthy
die "ScrumMaster did not answer /health within 90s"
