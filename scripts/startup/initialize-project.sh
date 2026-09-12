#!/usr/bin/env bash
# Step 6 — initialize the configured project.
#
# This is scripts/init-project.sh, the same script an operator runs by
# hand, driven from the configuration instead of from prompts: the name,
# deployment target and stack profile come from ai-gang.config.json, the
# repository URL from the same file's `repository.url`, and the
# fine-grained PAT from the platform .env, which the script reads as
# HQ_ENV. Nothing here re-decides any of them.
#
# Re-running is safe: the script's own per-project configuration-identity
# record resumes an existing project idempotently and refuses one whose
# decisions changed.

set -euo pipefail
STARTUP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$STARTUP_DIR/lib.sh"

begin_step initialize-project
require_command node
require_command jq
require_command git

load_decisions

log "initializing project '$PROJECT_NAME' ($PROJECT_TYPE / $PROJECT_STACK) against $REPOSITORY_URL"

# init-project.sh reads credentials from HQ_ENV. The platform .env is that
# file for this flow — one environment file, as REQ requires, rather than
# a second copy under $HOME.
HQ_ENV="$AIGANG_ENV_FILE" bash "$AIGANG_ROOT/scripts/init-project.sh" --config "$AIGANG_CONFIG_FILE" < /dev/null \
  || die "project initialization failed"

PROJECT_DIR="$AIGANG_ROOT/projects/$PROJECT_NAME"
[[ -d "$PROJECT_DIR" ]] || die "expected $PROJECT_DIR to exist after initialization"

log "project directory is $PROJECT_DIR"
end_step initialize-project
