#!/usr/bin/env bash
# forced-command.sh
#
# The ONLY thing the beta-deploy SSH key can run — see authorized_keys.example.
# Validates $SSH_ORIGINAL_COMMAND against a fixed allowlist of operations and
# argument shapes, then dispatches. Anything that doesn't match is refused.
# No shell metacharacters from the caller ever reach a subshell unquoted.

set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# PREVIEW_DOMAIN/BETA_DOMAIN aren't secrets (they're public hostnames) but
# preview-deploy.sh/deploy.sh need them in-process — this SSH session has no
# login shell/profile to source them from otherwise. See beta-vm/README.md
# step 2 for where this file comes from.
CONFIG_FILE="$DEPLOY_DIR/env"
if [[ -f "$CONFIG_FILE" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$CONFIG_FILE"
  set +a
fi

CMD="${SSH_ORIGINAL_COMMAND:-}"

PROJECT_RE='^[a-z][a-z0-9-]{1,40}$'
SHA_RE='^[0-9a-f]{7,40}$'
ISSUE_KEY_RE='^[A-Z][A-Z0-9]{1,9}-[0-9]+$'

# shellcheck disable=SC2206
ARGS=($CMD)
OP="${ARGS[0]:-}"

case "$OP" in
  deploy)
    PROJECT="${ARGS[1]:-}"; SHA="${ARGS[2]:-}"
    [[ "$PROJECT" =~ $PROJECT_RE && "$SHA" =~ $SHA_RE ]] || { echo "Refused: bad arguments" >&2; exit 1; }
    exec "$DEPLOY_DIR/deploy.sh" "$PROJECT" "$SHA"
    ;;
  preview-deploy)
    PROJECT="${ARGS[1]:-}"; SHA="${ARGS[2]:-}"; ISSUE_KEY="${ARGS[3]:-}"
    [[ "$PROJECT" =~ $PROJECT_RE && "$SHA" =~ $SHA_RE && "$ISSUE_KEY" =~ $ISSUE_KEY_RE ]] \
      || { echo "Refused: bad arguments" >&2; exit 1; }
    exec "$DEPLOY_DIR/preview-deploy.sh" "$PROJECT" "$SHA" "$ISSUE_KEY"
    ;;
  preview-teardown)
    PROJECT="${ARGS[1]:-}"; SHA="${ARGS[2]:-}"
    [[ "$PROJECT" =~ $PROJECT_RE && "$SHA" =~ $SHA_RE ]] || { echo "Refused: bad arguments" >&2; exit 1; }
    exec "$DEPLOY_DIR/preview-teardown.sh" "$PROJECT" "$SHA"
    ;;
  preview-teardown-by-issue)
    PROJECT="${ARGS[1]:-}"; ISSUE_KEY="${ARGS[2]:-}"
    [[ "$PROJECT" =~ $PROJECT_RE && "$ISSUE_KEY" =~ $ISSUE_KEY_RE ]] || { echo "Refused: bad arguments" >&2; exit 1; }
    exec "$DEPLOY_DIR/preview-teardown-by-issue.sh" "$PROJECT" "$ISSUE_KEY"
    ;;
  *)
    echo "Refused: unknown operation '${OP}'" >&2
    exit 1
    ;;
esac
