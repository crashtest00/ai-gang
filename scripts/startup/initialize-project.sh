#!/usr/bin/env bash
# Step 6 — initialize the configured project.
#
# This is scripts/init-project.sh, the same script an operator runs by
# hand, driven from the configuration instead of from prompts: the name,
# deployment target and stack profile come from ai-gang.config.json, the
# repository URL from the same file's `repository.url`, and the
# fine-grained PAT and the Claude credential from the platform .env. Nothing here
# re-decides any of them.
#
# The two credentials (the PAT, and the Claude credential the configured
# authentication method requires) are read out of the platform .env the way every
# other step reads it — as data, through env_file_get — and handed over
# in the environment. The file itself is never executed: a password
# holding a $, a backtick or a $(...) is a value, not a command.
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

# The platform .env is this flow's one environment file — no second copy
# under $HOME — so it is where both credentials come from. The Claude
# credential's name is the configured method's, taken from env-contract.sh
# through auth_credential_name. HQ_ENV still
# names it, because init-project.sh's own diagnostics tell the operator
# which file to fix.
GH_TOKEN="$(env_value GH_TOKEN)"
CREDENTIAL_VAR="$(auth_credential_name "$AUTH_METHOD")"
printf -v "$CREDENTIAL_VAR" '%s' "$(env_value "$CREDENTIAL_VAR")"
export GH_TOKEN "$CREDENTIAL_VAR"

AIGANG_CONFIG_FILE="$AIGANG_CONFIG_FILE" HQ_ENV="$AIGANG_ENV_FILE" bash "$AIGANG_ROOT/scripts/init-project.sh" --config "$AIGANG_CONFIG_FILE" < /dev/null \
  || die "project initialization failed"

PROJECT_DIR="$AIGANG_ROOT/projects/$PROJECT_NAME"
[[ -d "$PROJECT_DIR" ]] || die "expected $PROJECT_DIR to exist after initialization"

log "project directory is $PROJECT_DIR"
end_step initialize-project
