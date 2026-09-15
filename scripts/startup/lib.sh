#!/usr/bin/env bash
# Shared helpers for the platform startup steps. Sourced, never executed.
#
# Every step script under scripts/startup/ sources this, so the checkout
# root, the logging format, the .env reader and the status-record calls
# are defined once.

# AIGANG_ROOT is the operator's checkout — the directory holding
# docker-compose.yml, .env and ai-gang.config.json. Inside the AI Gang
# container it is the same absolute path as on the host, because the
# checkout is mounted at its own path (see the root docker-compose.yml).
# AIGANG_ROOT, AIGANG_ENV_FILE, AIGANG_CONFIG_FILE and AIGANG_STATE_DIR
# are all overridable so the test suite can drive these steps against a
# temporary tree instead of the real checkout — the same reason
# scripts/init-project.sh takes AIGANG_PROJECTS_DIR and
# AIGANG_PROJECTS_CONFIG.
STARTUP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AIGANG_ROOT="${AIGANG_ROOT:-$(cd "$STARTUP_DIR/../.." && pwd)}"

AIGANG_ENV_FILE="${AIGANG_ENV_FILE:-$AIGANG_ROOT/.env}"
AIGANG_ENV_TEMPLATE="${AIGANG_ENV_TEMPLATE:-$AIGANG_ROOT/.env.template}"
AIGANG_CONFIG_FILE="${AIGANG_CONFIG_FILE:-$AIGANG_ROOT/ai-gang.config.json}"
AIGANG_STATE_DIR="${AIGANG_STATE_DIR:-$AIGANG_ROOT/.ai-gang}"
AIGANG_STATUS_FILE="$AIGANG_STATE_DIR/status.json"
AIGANG_IDENTITY_FILE="$AIGANG_STATE_DIR/config-identity.json"

# The one address an operator opens. The work-item service's `api` service
# publishes 127.0.0.1:9100 (services/work-item-service/docker-compose.yml)
# and Django's admin site is mounted at /django-admin/
# (services/work-item-service/workitemservice/urls.py). status.sh holds
# the value, since it is what writes it into the status record.
AIGANG_ADMIN_URL="$("$STARTUP_DIR/status.sh" admin-url)"

# Every step's log goes to two places: the caller's own output, and
# .ai-gang/startup.log. The AI Gang container tails that file to its
# stdout while the Initialization Agent works, which is how a
# `docker compose up` in the foreground shows each step as it happens —
# the agent's own tool output never reaches the container's stdout.
AIGANG_LOG_FILE="$AIGANG_STATE_DIR/startup.log"

# Where the run before this one is kept. A run's records are the only
# account of what it did, and they outlive the container that wrote them:
# they are in the operator's checkout, outside every container, and
# nothing in this flow deletes them. Starting a new run would overwrite
# them, so the previous run's status record and log are moved here first
# — which is what makes the evidence of a failed run survive the re-run
# that follows it.
AIGANG_PREVIOUS_DIR="$AIGANG_STATE_DIR/previous"

# Moves the previous run's records aside, if there are any. Called once,
# by the entrypoint, before a new run's record is created.
keep_previous_run() {
  if [[ ! -f "$AIGANG_STATUS_FILE" && ! -f "$AIGANG_LOG_FILE" ]]; then
    return 0
  fi
  mkdir -p "$AIGANG_PREVIOUS_DIR"
  if [[ -f "$AIGANG_STATUS_FILE" ]]; then
    mv -f "$AIGANG_STATUS_FILE" "$AIGANG_PREVIOUS_DIR/status.json"
  fi
  if [[ -f "$AIGANG_LOG_FILE" ]]; then
    mv -f "$AIGANG_LOG_FILE" "$AIGANG_PREVIOUS_DIR/startup.log"
  fi
  return 0
}

log_line() {
  local line="$1"
  if [[ -d "$AIGANG_STATE_DIR" ]]; then
    printf '%s\n' "$line" >> "$AIGANG_LOG_FILE" 2>/dev/null || true
  fi
}

# While the AI Gang container is tailing the log file to its own stdout,
# that tail is the only path to it: printing here as well would show the
# operator every line twice. Run anywhere else — by hand, by the agent as
# a tool call, by a test — the log file is not being tailed and these
# print directly.
log() {
  local line
  line="$(printf '[startup] %s' "$*")"
  [[ "${AIGANG_LOG_TAILED:-0}" == "1" ]] || printf '%s\n' "$line"
  log_line "$line"
}

warn() {
  local line
  line="$(printf '[startup] %s' "$*")"
  [[ "${AIGANG_LOG_TAILED:-0}" == "1" ]] || printf '%s\n' "$line" >&2
  log_line "$line"
}

# Ends the run. The message goes to stderr and to the status record, so a
# reader of either learns the same thing.
die() {
  warn "ERROR: $*"
  if [[ -d "$AIGANG_STATE_DIR" ]]; then
    "$STARTUP_DIR/status.sh" fail "$*" || true
  fi
  exit 1
}

require_command() {
  local cmd="$1"
  command -v "$cmd" >/dev/null 2>&1 || die "required command '$cmd' is not on PATH"
}

# Reads a KEY=value file without executing it — a configuration or
# environment file is data. Values keep their exact bytes, except that a
# single matching pair of surrounding quotes is removed, which is what
# Docker Compose's own env_file reader does.
env_file_get() {
  local file="$1" name="$2"
  [[ -f "$file" ]] || return 1
  local line value
  line="$(grep -E "^[[:space:]]*${name}=" "$file" | tail -n 1 || true)"
  [[ -n "$line" ]] || return 1
  value="${line#*=}"
  if [[ ${#value} -ge 2 && "${value:0:1}" == '"' && "${value: -1}" == '"' ]]; then
    value="${value:1:${#value}-2}"
  elif [[ ${#value} -ge 2 && "${value:0:1}" == "'" && "${value: -1}" == "'" ]]; then
    value="${value:1:${#value}-2}"
  fi
  printf '%s' "$value"
}

# The effective value of a platform .env variable: what .env says, or the
# contract's default when .env is silent and the contract has one.
env_value() {
  local name="$1" value
  if value="$(env_file_get "$AIGANG_ENV_FILE" "$name")"; then
    if [[ -n "$value" ]]; then
      printf '%s' "$value"
      return 0
    fi
  fi
  local kind var default
  while read -r kind var default; do
    if [[ "$kind" == "OPTIONAL" && "$var" == "$name" ]]; then
      printf '%s' "$default"
      return 0
    fi
  done < <("$STARTUP_DIR/env-contract.sh")
  printf ''
}

status() {
  "$STARTUP_DIR/status.sh" "$@"
}

# Announces a step and marks it in progress. Every step script calls this
# once, first, with its own id, so the log and the status record agree on
# where the run is.
begin_step() {
  local id="$1"
  local number description
  number="$("$STARTUP_DIR/steps.sh" number "$id")" || die "unknown startup step '$id'"
  description="$("$STARTUP_DIR/steps.sh" describe "$id")"
  log "step ${number}: ${description}"
  status step-start "$id"
}

end_step() {
  local id="$1"
  status step-done "$id"
}

# The four configured values, read back through the same validator that
# gated the run. Steps call this rather than parsing the configuration
# themselves, so no step can act on a value the validator never approved.
# Sets PROJECT_NAME, PROJECT_TYPE, PROJECT_STACK and REPOSITORY_URL.
load_decisions() {
  local output key value
  output="$("$STARTUP_DIR/validate-config.sh")" || exit 1
  PROJECT_NAME=""; PROJECT_TYPE=""; PROJECT_STACK=""; REPOSITORY_URL=""
  while IFS='=' read -r key value; do
    case "$key" in
      PROJECT_NAME) PROJECT_NAME="$value" ;;
      PROJECT_TYPE) PROJECT_TYPE="$value" ;;
      PROJECT_STACK) PROJECT_STACK="$value" ;;
      REPOSITORY_URL) REPOSITORY_URL="$value" ;;
    esac
  done <<< "$output"
  [[ -n "$PROJECT_NAME" && -n "$PROJECT_TYPE" && -n "$PROJECT_STACK" && -n "$REPOSITORY_URL" ]] \
    || die "configuration validation did not produce the expected decisions"
}

# `docker compose` run exactly where an operator would run it: from the
# service's own directory, so the project name, the .env it interpolates
# and every relative bind mount are what that compose file expects.
compose_in() {
  local dir="$1"; shift
  ( cd "$AIGANG_ROOT/$dir" && docker compose "$@" )
}

# The Django API of the work-item service, answering its own health
# endpoint from inside its container. Both the step that restarts it and
# the step that confirms health ask the same question the same way.
workitem_api_healthy() {
  docker exec work-item-service curl -fsS http://localhost:9100/health
}

# One project's command channel in the work-item service: the stream its
# commands are written to, and the consumer group its consumers read them
# through (services/work-item-service/workitems/stream_topology.py —
# command_stream_name and COMMAND_GROUP).
WORKITEM_COMMAND_GROUP="workitemservice"

workitem_command_stream() {
  printf 'aigang:workitems:%s' "$1"
}

# True when the work-item service is actually consuming a project's
# commands. Its consumers process creates that project's consumer group
# as it starts (workitems/streams.py, XGROUP CREATE with MKSTREAM), so
# the group's presence is the one observable fact that says the project
# is being served. Without it, every command written for that project
# waits on the stream undelivered and nothing anywhere reports an error.
#
# `grep -x` here, not `grep -qx`: XINFO GROUPS lists the target group
# alongside sibling fields (consumers, pending, and more, after it), so
# a quiet grep that stops reading the moment it finds the group can still
# be mid-pipe when redis-cli or the group's own writer is scheduled back
# in to send the rest — closing its end of the pipe under it and handing
# it SIGPIPE. Under `set -o pipefail` that turns a group that was found
# into a wait failure: the pipeline reports the producer's 141, not
# grep's own success. Reading to EOF, with output thrown away instead of
# suppressed, removes the only thing pipefail had to complain about.
workitem_commands_consumed() {
  local project="$1"
  docker exec ai-gang-redis redis-cli XINFO GROUPS "$(workitem_command_stream "$project")" 2>/dev/null \
    | grep -x "$WORKITEM_COMMAND_GROUP" >/dev/null
}

# Retries a condition until it holds, or gives up. The attempt count is
# the caller's; both it and the pause between attempts are overridable
# through the environment, so a test can drive a wait that never succeeds
# without sitting out the real timeout — the same reason AIGANG_ROOT and
# AIGANG_STATE_DIR are overridable.
wait_until() {
  local attempts="${AIGANG_WAIT_ATTEMPTS:-$1}"; shift
  local interval="${AIGANG_WAIT_INTERVAL:-2}"
  local attempt
  for ((attempt = 0; attempt < attempts; attempt++)); do
    if "$@" >/dev/null 2>&1; then
      return 0
    fi
    sleep "$interval"
  done
  return 1
}
