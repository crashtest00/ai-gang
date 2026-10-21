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
# The canonical work-item id — a UUID, in every mode (V5.2 Canonical
# Delivery State REQ-08). Replaces the Jira-key-shaped pattern this used to
# validate; no tracker key reaches this script in either mode now.
WORK_ITEM_ID_RE='^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'

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
    PROJECT="${ARGS[1]:-}"; SHA="${ARGS[2]:-}"; WORK_ITEM_ID="${ARGS[3]:-}"
    [[ "$PROJECT" =~ $PROJECT_RE && "$SHA" =~ $SHA_RE && "$WORK_ITEM_ID" =~ $WORK_ITEM_ID_RE ]] \
      || { echo "Refused: bad arguments" >&2; exit 1; }
    exec "$DEPLOY_DIR/preview-deploy.sh" "$PROJECT" "$SHA" "$WORK_ITEM_ID"
    ;;
  preview-teardown)
    PROJECT="${ARGS[1]:-}"; SHA="${ARGS[2]:-}"
    [[ "$PROJECT" =~ $PROJECT_RE && "$SHA" =~ $SHA_RE ]] || { echo "Refused: bad arguments" >&2; exit 1; }
    exec "$DEPLOY_DIR/preview-teardown.sh" "$PROJECT" "$SHA"
    ;;
  preview-teardown-by-work-item)
    PROJECT="${ARGS[1]:-}"; WORK_ITEM_ID="${ARGS[2]:-}"
    [[ "$PROJECT" =~ $PROJECT_RE && "$WORK_ITEM_ID" =~ $WORK_ITEM_ID_RE ]] || { echo "Refused: bad arguments" >&2; exit 1; }
    exec "$DEPLOY_DIR/preview-teardown-by-work-item.sh" "$PROJECT" "$WORK_ITEM_ID"
    ;;
  *)
    echo "Refused: unknown operation '${OP}'" >&2
    exit 1
    ;;
esac
