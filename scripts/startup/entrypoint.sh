#!/usr/bin/env bash
# The AI Gang container's entrypoint: the whole of what `docker compose up`
# at the repository root does.
#
# Order, and why it is this order:
#
#   1. Drop to the checkout's own owner. The container starts as root so
#      it can reach the daemon socket, then runs everything else as
#      whoever owns the mounted checkout, with the socket's group added.
#      Without this, every file initialization creates — the project
#      directory, the derived environment files, the status record —
#      would come back to the operator owned by root.
#   2. Validate the configuration and the environment file. Nothing else
#      may happen before these two pass: an invalid configuration must
#      cost the operator a diagnostic, not a half-built installation.
#   3. Compare the configuration against what this checkout was
#      initialized with, and refuse a changed one before anything moves.
#   4. Derive each service's environment file from the platform .env.
#   5. Hand the ordered steps to the Initialization Agent, unsupervised.
#   6. Check the status record the steps maintain, and exit on what it
#      says rather than on the agent's word.
#
# The container has no role after that. It exits, and does not restart.

set -euo pipefail

# This file is the only thing baked into the image; every step it runs
# comes from the checkout mounted at the working directory, so that an
# operator's own checkout is what executes. That is why the step scripts
# are found relative to $PWD and not relative to this file.
AIGANG_ROOT="${AIGANG_ROOT:-$PWD}"
STARTUP_DIR="$AIGANG_ROOT/scripts/startup"

if [[ ! -f "$STARTUP_DIR/lib.sh" ]]; then
  echo "[startup] ERROR: $STARTUP_DIR does not look like an AI Gang checkout." >&2
  echo "[startup] Run 'docker compose up' from the root of the AI Gang repository." >&2
  exit 1
fi

# --- 1. run as the checkout's owner ------------------------------------
# Re-execs itself once, so everything below this block runs unprivileged.
if [[ "${AIGANG_STARTUP_UNPRIVILEGED:-0}" != "1" && "$(id -u)" == "0" ]]; then
  checkout="$PWD"
  target_uid="$(stat -c %u "$checkout")"
  target_gid="$(stat -c %g "$checkout")"

  if [[ "$target_uid" != "0" ]]; then
    socket_gid=""
    if [[ -S /var/run/docker.sock ]]; then
      socket_gid="$(stat -c %g /var/run/docker.sock)"
    fi

    getent group "$target_gid" >/dev/null 2>&1 || groupadd -g "$target_gid" aigang
    if ! getent passwd "$target_uid" >/dev/null 2>&1; then
      useradd -u "$target_uid" -g "$target_gid" -M -d /home/aigang -s /bin/bash aigang
    fi
    mkdir -p /home/aigang
    chown "$target_uid:$target_gid" /home/aigang

    groups_arg="$target_gid"
    if [[ -n "$socket_gid" && "$socket_gid" != "$target_gid" ]]; then
      getent group "$socket_gid" >/dev/null 2>&1 || groupadd -g "$socket_gid" aigang-docker
      groups_arg="$groups_arg,$socket_gid"
    fi

    export AIGANG_STARTUP_UNPRIVILEGED=1
    export HOME=/home/aigang
    exec setpriv --reuid "$target_uid" --regid "$target_gid" --groups "$groups_arg" \
      --inh-caps=-all "${BASH_SOURCE[0]}" "$@"
  else
    # The checkout is owned by root, so there is nobody to drop to and
    # everything below runs as root: Claude Code, docker, git, and every
    # step. That is a real difference in what the run creates, so it is
    # said out loud in the run's own log rather than left to be inferred.
    AIGANG_ROOT_CHECKOUT=1
  fi
fi

# shellcheck source=lib.sh
source "$STARTUP_DIR/lib.sh"

export HOME="${HOME:-/home/aigang}"

# Keep the previous run's record and log before this run writes a line of
# its own. Nothing in this flow deletes a run's records: a failed run's
# evidence has to still be there after the re-run that follows it.
keep_previous_run

log "AI Gang platform startup"
log "checkout: $AIGANG_ROOT"

require_command docker
require_command node
require_command jq
require_command git
require_command claude

mkdir -p "$AIGANG_STATE_DIR"
status init
: > "$AIGANG_LOG_FILE"

# The container's stdout is the operator's only view of a run in the
# foreground, and the agent's tool output does not reach it. Tailing the
# steps' own log does.
tail -n +1 -F "$AIGANG_LOG_FILE" &
TAIL_PID=$!
export AIGANG_LOG_TAILED=1

# Stops the tail and hands stdout back, so the last lines of the run are
# printed once and are not lost to a killed tail. `tail -F` has to notice
# a write and read it before it can print it, so killing it the instant a
# step fails loses that step's diagnostic — measured on the validation
# failures below, the named field reached the terminal in only half of
# twenty runs. Every exit goes through here, not just the successful one:
# that is what the EXIT trap is for, and why it calls this rather than
# killing the tail itself.
stop_log_tail() {
  local pid="${TAIL_PID:-}"
  [[ -n "$pid" ]] || return 0
  TAIL_PID=""
  trap - EXIT
  sleep 1
  kill "$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
  export AIGANG_LOG_TAILED=0
  return 0
}
trap stop_log_tail EXIT

if [[ -d "$AIGANG_PREVIOUS_DIR" ]]; then
  log "the previous run's record and log are kept in $AIGANG_PREVIOUS_DIR"
fi

if [[ "${AIGANG_ROOT_CHECKOUT:-0}" == "1" ]]; then
  warn "this checkout is owned by root, so there is no unprivileged user to drop to:"
  warn "  the whole run, including the Initialization Agent, runs as root, and every"
  warn "  file it creates in $AIGANG_ROOT will be owned by root."
fi

# --- 2. validate -------------------------------------------------------
status phase preflight
log "validating $AIGANG_CONFIG_FILE"
"$STARTUP_DIR/validate-config.sh" >/dev/null
log "configuration is valid"

log "validating $AIGANG_ENV_FILE"
"$STARTUP_DIR/validate-env.sh"

# --- 3. this checkout's recorded configuration -------------------------
"$STARTUP_DIR/config-identity.sh" check

# --- 4. derive each service's environment file -------------------------
"$STARTUP_DIR/derive-env.sh"

# The record is written only once validation and derivation have passed,
# so a run that never got as far as touching the daemon leaves no claim
# behind that this checkout was initialized.
"$STARTUP_DIR/config-identity.sh" record

# --- 5. the Initialization Agent ---------------------------------------
PROMPT_FILE="$(mktemp)"
{
  cat <<'PREAMBLE'
You are the Initialization Agent bringing the AI Gang platform up on this
machine, unsupervised. No human is watching and no one will answer a
question, so do not ask one.

The working directory is the AI Gang checkout. Its configuration
(ai-gang.config.json) and its environment file (.env) have already been
validated; the project name, deployment target, stack profile and
repository URL they carry are settled. Do not re-read them to reconsider
any of those four values, do not ask for them, and do not change them.

Read the "Platform Startup" section of docs/ClaudeInstructions.md, then
run these steps, in this order, one at a time, from the checkout root:

PREAMBLE
  "$STARTUP_DIR/steps.sh" list | while IFS='|' read -r number id script description; do
    printf '  %s. ./%s   — %s\n' "$number" "$script" "$description"
  done
  cat <<'RULES'

Each script is idempotent and reports what it did. After each one, check
its exit status before starting the next.

Your judgment is for a step that fails, and for nothing else. A routine
step is the script's job, not a decision of yours. When a step fails:

  - Diagnose it from its own output and from `docker logs` / `docker ps`.
  - If the cause is something you can genuinely put right — a transient
    pull failure, a container that needs one more moment, a stale
    container from an earlier run — put it right and re-run that same
    script. The scripts are safe to re-run.
  - Never build a service by hand, never write a compose file, a
    Dockerfile or an environment file yourself, and never substitute a
    different image, name or port for the one a script uses. A service
    that cannot be started by its script is a failure to report, not a
    thing to assemble.
  - If you cannot put it right, stop. Run
    ./scripts/startup/status.sh fail "<one-line reason>" and say plainly
    what failed and what you observed. A clean failure is the correct
    outcome; a hand-built substitute is not.

When the last step reports the platform is up, stop. You are done.
RULES
} > "$PROMPT_FILE"

status phase initializing
log "handing the ordered steps to the Initialization Agent"

set +e
claude --print --dangerously-skip-permissions "$(cat "$PROMPT_FILE")"
AGENT_EXIT=$?
set -e
rm -f "$PROMPT_FILE"
log "Initialization Agent exited with status $AGENT_EXIT"

# --- 6. exit on the record, not on the agent's word --------------------
stop_log_tail
RUN_STATE="$(status state 2>/dev/null || echo unknown)"
if [[ "$RUN_STATE" == "complete" ]]; then
  log "initialization complete. Django admin: $AIGANG_ADMIN_URL"
  exit 0
fi

if [[ "$RUN_STATE" == "in-progress" ]]; then
  status fail "the Initialization Agent stopped before the last step completed"
fi

warn "initialization did not complete — status is '$RUN_STATE'"
warn "see $AIGANG_STATUS_FILE and $AIGANG_LOG_FILE"
exit 1
