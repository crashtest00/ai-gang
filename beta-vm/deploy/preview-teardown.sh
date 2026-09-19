#!/usr/bin/env bash
# preview-teardown.sh <project> <sha>
#
# Torn down after the Release ticket resolves (approved or abandoned).
# Called by the production-promote job once it has redeployed the same
# artifact to prod.

set -euo pipefail
PROJECT="$1"
SHA="$2"
CONTAINER="preview-$PROJECT-$SHA"

docker rm -f "$CONTAINER" > /dev/null 2>&1 || true
echo "Torn down $CONTAINER"
