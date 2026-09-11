#!/usr/bin/env bash
# preview-teardown-by-issue.sh <project> <issue-key>
#
# Used when a Release ticket is abandoned — ScrumMaster's handleReleaseAbandoned
# knows the Release ticket key but not necessarily the candidate SHA (a
# release can be abandoned before or after a candidate was ever cut). Finds
# the preview container by its `issue` label instead.

set -euo pipefail
PROJECT="$1"
ISSUE_KEY="$2"

MATCHES=$(docker ps -a --filter "label=issue=$ISSUE_KEY" --filter "name=preview-$PROJECT-" --format '{{.Names}}')

if [[ -z "$MATCHES" ]]; then
  echo "No preview container found for $PROJECT / $ISSUE_KEY — nothing to do."
  exit 0
fi

while IFS= read -r name; do
  docker rm -f "$name" > /dev/null 2>&1 || true
  echo "Torn down $name"
done <<< "$MATCHES"
