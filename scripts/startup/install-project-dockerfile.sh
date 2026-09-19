#!/usr/bin/env bash
# Step 7 — install the project container's Dockerfile from the template
# its configured stack profile selects.
#
# scripts/init-project.sh writes the project's docker-compose.yml with
# `build: .` but no Dockerfile, and tells the operator to copy one in by
# hand. That hand step is the last thing between a validated
# configuration and a running project container, so the configured stack
# selects the template instead and this copies it.
#
# An existing Dockerfile is left alone: a project whose container was
# customised must survive a re-run untouched.

set -euo pipefail
STARTUP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$STARTUP_DIR/lib.sh"

begin_step install-project-dockerfile

load_decisions

PROJECT_DIR="$AIGANG_ROOT/projects/$PROJECT_NAME"
[[ -d "$PROJECT_DIR" ]] || die "project directory $PROJECT_DIR does not exist — run the project initialization step first"

TEMPLATE_REL="$("$STARTUP_DIR/stack-dockerfile.sh" "$PROJECT_TYPE" "$PROJECT_STACK")" \
  || die "no container image template for $PROJECT_TYPE / $PROJECT_STACK"
TEMPLATE_PATH="$AIGANG_ROOT/$TEMPLATE_REL"
[[ -f "$TEMPLATE_PATH" ]] || die "container image template missing at $TEMPLATE_PATH"

if [[ -f "$PROJECT_DIR/Dockerfile" ]]; then
  log "$PROJECT_DIR/Dockerfile already exists — leaving it as it is"
else
  cp "$TEMPLATE_PATH" "$PROJECT_DIR/Dockerfile"
  log "installed $TEMPLATE_REL as $PROJECT_DIR/Dockerfile"
fi

end_step install-project-dockerfile
