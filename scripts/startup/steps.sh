#!/usr/bin/env bash
# The ordered initialization steps, and the script that performs each one.
#
# This list is the contract between three things that would otherwise
# drift: the prompt the AI Gang container hands the Initialization Agent,
# the status record each step updates, and the platform-startup section of
# docs/ClaudeInstructions.md that a reader follows. All three are built
# from, or checked against, exactly this output.
#
# Usage:
#   steps.sh list              — "<number>|<id>|<script>|<description>" per step
#   steps.sh number <id>       — that step's position
#   steps.sh describe <id>     — that step's one-line description

set -euo pipefail

STEPS=(
  "create-network|Create the ai-gang Docker network if it does not already exist"
  "start-redis|Start Redis"
  "start-work-item-service|Build and start the work-item service and apply its database migrations"
  "create-admin|Create the Django admin account from .env"
  "start-scrummaster|Build and start ScrumMaster"
  "initialize-project|Initialize the configured project from the configuration"
  "install-project-dockerfile|Install the project container's Dockerfile from its stack's template"
  "start-project|Build and start the project container, then reload the services that read the project list at startup"
  "confirm-health|Confirm every service is healthy and record the admin address"
)

step_script() {
  printf 'scripts/startup/%s.sh' "$1"
}

cmd_list() {
  local i=1 entry id description
  for entry in "${STEPS[@]}"; do
    id="${entry%%|*}"
    description="${entry#*|}"
    printf '%d|%s|%s|%s\n' "$i" "$id" "$(step_script "$id")" "$description"
    i=$((i + 1))
  done
}

cmd_field() {
  local want="$1" field="$2"
  while IFS='|' read -r number id script description; do
    if [[ "$id" == "$want" ]]; then
      case "$field" in
        number) printf '%s' "$number" ;;
        describe) printf '%s' "$description" ;;
      esac
      return 0
    fi
  done < <(cmd_list)
  printf 'steps.sh: unknown step id "%s"\n' "$want" >&2
  return 1
}

case "${1:-list}" in
  list) cmd_list ;;
  number) cmd_field "${2:?steps.sh number <id>}" number ;;
  describe) cmd_field "${2:?steps.sh describe <id>}" describe ;;
  *) printf 'usage: steps.sh list|number <id>|describe <id>\n' >&2; exit 2 ;;
esac
