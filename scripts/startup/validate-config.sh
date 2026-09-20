#!/usr/bin/env bash
# Validates the platform configuration, ai-gang.config.json, before any
# service container exists. Nothing else in startup runs until this
# passes: an unknown field, a missing field, a value still at its
# ai-gang.config.template.json placeholder, or an unsupported deployment
# target or stack profile each stop the run here, named.
#
# On success the normalized decisions are printed as plain KEY=value
# lines, for a caller to read:
#   PROJECT_NAME, PROJECT_TYPE, PROJECT_STACK, REPOSITORY_URL
#
# The validation itself is setup/graphs/engine/lib/config/ — the same
# shared path scripts/init-project.sh --config uses, with the platform
# rules selected. There is one implementation of "what counts as valid".

set -euo pipefail

STARTUP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$STARTUP_DIR/lib.sh"

require_command node

if [[ ! -f "$AIGANG_CONFIG_FILE" ]]; then
  die "no platform configuration at $AIGANG_CONFIG_FILE — copy ai-gang.config.template.json to ai-gang.config.json and fill it in"
fi

CONFIG_CLI="$AIGANG_ROOT/setup/graphs/engine/lib/config/cli.js"
[[ -f "$CONFIG_CLI" ]] || die "configuration validator missing at $CONFIG_CLI"

STDERR_FILE="$(mktemp)"
if ! OUTPUT="$(node "$CONFIG_CLI" --platform "$AIGANG_CONFIG_FILE" 2>"$STDERR_FILE")"; then
  while IFS= read -r line; do
    warn "$line"
  done < "$STDERR_FILE"
  rm -f "$STDERR_FILE"
  die "$AIGANG_CONFIG_FILE is not a valid platform configuration"
fi
rm -f "$STDERR_FILE"

printf '%s\n' "$OUTPUT"
