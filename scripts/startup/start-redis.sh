#!/usr/bin/env bash
# Step 2 — start Redis, the message broker every other service talks
# through, using its own compose file unchanged.

set -euo pipefail
STARTUP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$STARTUP_DIR/lib.sh"

begin_step start-redis
require_command docker

compose_in services/redis up -d || die "Redis did not start"

for _ in $(seq 1 30); do
  if docker exec ai-gang-redis redis-cli ping 2>/dev/null | grep -q PONG; then
    log "Redis is answering PING"
    status service redis healthy
    end_step start-redis
    exit 0
  fi
  sleep 2
done

status service redis unhealthy
die "Redis did not answer PING within 60s"
