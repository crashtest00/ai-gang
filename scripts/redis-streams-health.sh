#!/usr/bin/env bash
set -euo pipefail

# Operator diagnostic for the Redis Streams transport.
#
# Usage:
#   scripts/redis-streams-health.sh <project-name> [agent-suffix ...]
#
# With no suffixes given, defaults to the standard four dev-agent roles.
# Prefer ScrumMaster's GET /health for a machine-readable summary across
# every configured project — this script is for ad-hoc, one-stream-at-a-time
# inspection (e.g. while debugging a specific stuck ticket).

PROJECT="${1:?Usage: $0 <project-name> [agent-suffix ...]}"
shift || true
SUFFIXES=("$@")
if [ "${#SUFFIXES[@]}" -eq 0 ]; then
  SUFFIXES=(refinement backend frontend devops)
fi

REDIS_CONTAINER="${REDIS_CONTAINER:-ai-gang-redis}"
PROJECT_LC=$(echo "$PROJECT" | tr '[:upper:]' '[:lower:]')

rcli() {
  docker exec "$REDIS_CONTAINER" redis-cli "$@"
}

inspect_stream() {
  local stream="$1" group="$2"
  echo "=== $stream (group: $group) ==="
  echo "-- XLEN --"
  rcli XLEN "$stream" || echo "(stream does not exist yet)"
  echo "-- XINFO GROUPS --"
  rcli XINFO GROUPS "$stream" || echo "(no groups yet)"
  echo "-- XPENDING summary --"
  rcli XPENDING "$stream" "$group" || echo "(group not found)"
  echo "-- Dead-letter length ($stream:dead) --"
  rcli XLEN "$stream:dead" || echo 0
  echo
}

echo "Redis Streams health for project: $PROJECT_LC"
echo

inspect_stream "aigang:webhooks:$PROJECT_LC" "scrummaster"
inspect_stream "aigang:gateway:$PROJECT_LC" "scrummaster"

for suffix in "${SUFFIXES[@]}"; do
  inspect_stream "aigang:agent:$PROJECT_LC:$suffix" "agent-$suffix"
done

cat <<'EOF'
Reading further:
  XRANGE <stream> - +                                dump every retained entry
  XPENDING <stream> <group> - + 10                   list up to 10 pending entries with idle time
  (cd services/scrummaster && node scripts/redis-streams-replay.js <dead-stream> <entry-id>)
                                                       replay a dead-lettered entry
EOF
