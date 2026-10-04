#!/usr/bin/env bash
# preview-teardown-by-work-item.sh <project> <work-item-id>
#
# Used when a Release work item is abandoned — ScrumMaster's
# handleReleaseAbandoned knows the Release's canonical work-item id but not
# necessarily the candidate SHA (a release can be abandoned before or after
# a candidate was ever cut). Finds the preview container by its
# `work_item` label instead.

set -euo pipefail
PROJECT="$1"
WORK_ITEM_ID="$2"

MATCHES=$(docker ps -a --filter "label=work_item=$WORK_ITEM_ID" --filter "name=preview-$PROJECT-" --format '{{.Names}}')

if [[ -z "$MATCHES" ]]; then
  echo "No preview container found for $PROJECT / $WORK_ITEM_ID — nothing to do."
  exit 0
fi

while IFS= read -r name; do
  docker rm -f "$name" > /dev/null 2>&1 || true
  echo "Torn down $name"
done <<< "$MATCHES"
