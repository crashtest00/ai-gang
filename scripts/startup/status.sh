#!/usr/bin/env bash
# The startup status record: .ai-gang/status.json in the operator's
# checkout, outside every container, so it can be read while the run is in
# progress and after it ends — from a second shell on the host, without
# attaching to anything.
#
# It names the step in progress, every step's state, each service's
# health, and, once the run completes, the Django admin address. It never
# holds a credential: every value written here comes from the step list,
# a service name, or a diagnostic message.
#
# Usage:
#   status.sh init                     — create/reset the record for a new run
#   status.sh step-start <id>          — mark a step in progress
#   status.sh step-done <id>           — mark a step complete
#   status.sh service <name> <state>   — record one service's health
#   status.sh phase <phase>            — preflight | initializing
#   status.sh complete                 — mark the run complete, name the admin address
#   status.sh fail <message>           — mark the run failed, with a reason
#   status.sh state                    — print just the run state
#   status.sh admin-url                — print the Django admin address

set -euo pipefail

STARTUP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AIGANG_ROOT="${AIGANG_ROOT:-$(cd "$STARTUP_DIR/../.." && pwd)}"
AIGANG_STATE_DIR="${AIGANG_STATE_DIR:-$AIGANG_ROOT/.ai-gang}"
STATUS_FILE="$AIGANG_STATE_DIR/status.json"
ADMIN_URL="http://127.0.0.1:9100/django-admin/"

command -v jq >/dev/null 2>&1 || { echo "status.sh requires 'jq' on PATH" >&2; exit 1; }

now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

write_json() {
  local tmp
  tmp="$(mktemp "$AIGANG_STATE_DIR/.status.XXXXXX")"
  cat > "$tmp"
  mv "$tmp" "$STATUS_FILE"
}

require_record() {
  [[ -f "$STATUS_FILE" ]] || { echo "status.sh: no status record at $STATUS_FILE — run 'status.sh init' first" >&2; exit 1; }
}

edit() {
  require_record
  jq "$@" "$STATUS_FILE" | write_json
}

cmd_init() {
  mkdir -p "$AIGANG_STATE_DIR"
  local steps_json
  steps_json="$("$STARTUP_DIR/steps.sh" list | jq -R -s '
    split("\n") | map(select(length > 0)) | map(split("|")) |
    map({number: (.[0] | tonumber), id: .[1], description: .[3], state: "pending",
         startedAt: null, finishedAt: null})')"
  jq -n --argjson steps "$steps_json" --arg at "$(now)" '{
    schemaVersion: 1,
    state: "in-progress",
    phase: "preflight",
    step: null,
    steps: $steps,
    services: {},
    adminUrl: null,
    error: null,
    startedAt: $at,
    updatedAt: $at
  }' | write_json
}

cmd_phase() {
  edit --arg phase "${1:?status.sh phase <phase>}" --arg at "$(now)" \
    '.phase = $phase | .updatedAt = $at'
}

cmd_step_start() {
  local id="${1:?status.sh step-start <id>}"
  # Starting a step clears an earlier failure. A step that failed and was
  # then put right and re-run leaves the run in progress again — without
  # this, the record would keep reporting the recovered failure as the
  # reason for whatever happened afterwards, which is how a live run came
  # to blame a Redis problem it had already fixed.
  edit --arg id "$id" --arg at "$(now)" '
    .state = "in-progress"
    | .phase = "initializing"
    | .error = null
    | .step = ($id)
    | .steps = (.steps | map(if .id == $id then (.state = "in-progress" | .startedAt = $at | .finishedAt = null) else . end))
    | .updatedAt = $at'
}

cmd_step_done() {
  local id="${1:?status.sh step-done <id>}"
  edit --arg id "$id" --arg at "$(now)" '
    .steps = (.steps | map(if .id == $id then (.state = "complete" | .finishedAt = $at) else . end))
    | .updatedAt = $at'
}

cmd_service() {
  local name="${1:?status.sh service <name> <state>}" state="${2:?status.sh service <name> <state>}"
  edit --arg name "$name" --arg state "$state" --arg at "$(now)" \
    '.services[$name] = $state | .updatedAt = $at'
}

# Completion is not a claim anybody may make: it follows from the record.
# The Initialization Agent can run this script, and the entrypoint exits
# on what the record says rather than on the agent's exit code — so an
# agent that stopped early could otherwise mark the run complete itself
# and make the container exit 0. Every step has to have finished first,
# and every step's own script is what marks it finished.
cmd_complete() {
  require_record
  local unfinished
  unfinished="$(jq -r '[.steps[] | select(.state != "complete") | .id] | join(", ")' "$STATUS_FILE")"
  if [[ -n "$unfinished" ]]; then
    echo "status.sh: refusing to mark the run complete — these steps have not completed: $unfinished" >&2
    exit 1
  fi
  edit --arg url "$ADMIN_URL" --arg at "$(now)" \
    '.state = "complete" | .phase = "complete" | .step = null | .adminUrl = $url | .error = null | .updatedAt = $at'
}

cmd_fail() {
  local message="${1:-initialization failed}"
  edit --arg message "$message" --arg at "$(now)" \
    '.state = "failed" | .phase = "failed" | .error = $message | .updatedAt = $at'
}

case "${1:-state}" in
  init) cmd_init ;;
  phase) shift; cmd_phase "$@" ;;
  step-start) shift; cmd_step_start "$@" ;;
  step-done) shift; cmd_step_done "$@" ;;
  service) shift; cmd_service "$@" ;;
  complete) cmd_complete ;;
  fail) shift; cmd_fail "$@" ;;
  admin-url) printf '%s\n' "$ADMIN_URL" ;;
  state) require_record; jq -r '.state' "$STATUS_FILE" ;;
  *) echo "usage: status.sh init|phase|step-start|step-done|service|complete|fail|state|admin-url" >&2; exit 2 ;;
esac
