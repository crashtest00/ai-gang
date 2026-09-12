#!/usr/bin/env bash
# The checkout-level record of the configuration this checkout was
# initialized with: .ai-gang/config-identity.json, written once, on the
# first successful run, and compared on every run after that. A run whose
# configuration differs is refused here, before anything is created or
# changed.
#
# This is platform-level and deliberately separate from the per-project
# record scripts/init-project.sh writes
# (projects/<name>/.aigang-config-identity.json). That one is keyed by the
# project directory, so a changed project.name simply addresses a
# different directory and looks like a brand-new project. Only a record
# held by the checkout can notice that the checkout has already been
# initialized under another name — the difference between refusing a
# changed configuration and silently initializing a second project.
#
# Usage:
#   config-identity.sh check   — compare, refusing a differing configuration
#   config-identity.sh record  — write the record (no-op if it matches)

set -euo pipefail

STARTUP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$STARTUP_DIR/lib.sh"

require_command jq

compare() {
  local existing_name existing_type existing_stack existing_url
  existing_name="$(jq -r '.project.name // ""' "$AIGANG_IDENTITY_FILE")"
  existing_type="$(jq -r '.project.type // ""' "$AIGANG_IDENTITY_FILE")"
  existing_stack="$(jq -r '.project.stack // ""' "$AIGANG_IDENTITY_FILE")"
  existing_url="$(jq -r '.repository.url // ""' "$AIGANG_IDENTITY_FILE")"

  if [[ "$existing_name" != "$PROJECT_NAME" || "$existing_type" != "$PROJECT_TYPE" \
     || "$existing_stack" != "$PROJECT_STACK" || "$existing_url" != "$REPOSITORY_URL" ]]; then
    warn "ERROR: this checkout was already initialized with a different configuration."
    warn "  recorded: name=$existing_name type=$existing_type stack=$existing_stack repository=$existing_url"
    warn "  current:  name=$PROJECT_NAME type=$PROJECT_TYPE stack=$PROJECT_STACK repository=$REPOSITORY_URL"
    warn "  Nothing has been changed. Restore the recorded configuration, or start from a"
    warn "  fresh checkout to initialize a different project."
    die "configuration differs from $AIGANG_IDENTITY_FILE"
  fi
}

write_record() {
  mkdir -p "$AIGANG_STATE_DIR"
  local tmp
  tmp="$(mktemp "$AIGANG_STATE_DIR/.identity.XXXXXX")"
  jq -n \
    --arg name "$PROJECT_NAME" --arg type "$PROJECT_TYPE" \
    --arg stack "$PROJECT_STACK" --arg url "$REPOSITORY_URL" \
    --arg at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    '{schemaVersion: 1, project: {name: $name, type: $type, stack: $stack},
      repository: {url: $url}, initializedAt: $at}' > "$tmp"
  mv "$tmp" "$AIGANG_IDENTITY_FILE"
}

case "${1:-check}" in
  check)
    load_decisions
    if [[ -f "$AIGANG_IDENTITY_FILE" ]]; then
      compare
      log "configuration matches $AIGANG_IDENTITY_FILE — resuming this checkout's existing installation"
    else
      log "no previous configuration recorded for this checkout — this is a first run"
    fi
    ;;
  record)
    load_decisions
    if [[ -f "$AIGANG_IDENTITY_FILE" ]]; then
      compare
    else
      write_record
      log "recorded this checkout's configuration in $AIGANG_IDENTITY_FILE"
    fi
    ;;
  *)
    warn "usage: config-identity.sh check|record"
    exit 2
    ;;
esac
