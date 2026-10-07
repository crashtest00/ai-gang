#!/usr/bin/env bash
# Validates the platform .env before any service container exists. Every
# variable scripts/startup/env-contract.sh marks REQUIRED must be present
# and filled in, and so must every REQUIRED_IN_METHOD variable whose method
# is the configured `authMethod`; one that is missing, empty, or still
# holding the value .env.template ships stops the run here and is named.
# A variable required only in another method is not read.
#
# Nothing is printed but diagnostics: a secret's value never reaches
# stdout, the log, or the status record.

set -euo pipefail

STARTUP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$STARTUP_DIR/lib.sh"

[[ -f "$AIGANG_ENV_FILE" ]] || die "no environment file at $AIGANG_ENV_FILE — copy .env.template to .env and fill it in"

# The configured authentication method, read through the same validator that
# gated the run (the entrypoint ran validate-config.sh before this step).
load_decisions

missing=()
placeholder=()

while read -r kind first second; do
  case "$kind" in
    REQUIRED) name="$first" ;;
    REQUIRED_IN_METHOD)
      [[ "$first" == "$AUTH_METHOD" ]] || continue
      name="$second"
      ;;
    *) continue ;;
  esac
  value=""
  if ! value="$(env_file_get "$AIGANG_ENV_FILE" "$name")"; then
    missing+=("$name")
    continue
  fi
  if [[ -z "$value" ]]; then
    missing+=("$name")
    continue
  fi
  # "Still at its placeholder" is decided against .env.template's own
  # value for the same variable, so the rule needs no second list to
  # maintain: whatever the template ships is what counts as unfilled.
  if template_value="$(env_file_get "$AIGANG_ENV_TEMPLATE" "$name")"; then
    if [[ -n "$template_value" && "$value" == "$template_value" ]]; then
      placeholder+=("$name")
    fi
  fi
done < <("$STARTUP_DIR/env-contract.sh")

if (( ${#missing[@]} > 0 )) || (( ${#placeholder[@]} > 0 )); then
  for name in "${missing[@]:-}"; do
    [[ -n "$name" ]] && warn "ERROR: required variable $name is missing or empty in $AIGANG_ENV_FILE"
  done
  for name in "${placeholder[@]:-}"; do
    [[ -n "$name" ]] && warn "ERROR: required variable $name is still at its .env.template placeholder in $AIGANG_ENV_FILE"
  done
  die "$AIGANG_ENV_FILE is incomplete — see the variables named above"
fi

log "environment file $AIGANG_ENV_FILE is complete"
