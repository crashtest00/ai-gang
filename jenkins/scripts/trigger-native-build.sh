#!/usr/bin/env bash
# Dispatches build-desktop.yml for CANDIDATE_SHA when the current directory is
# a desktop-lane project (has .github/workflows/build-desktop.yml), waits for
# the run to finish, and prints:
#   STATUS=passed|failed|skipped|unknown
#   URL=<github actions run url, or empty>
# for the calling Jenkins pipeline stage to capture via `sh(returnStdout: true)`.
#
# Native validation is supplementary — the web/Beta preview is the default
# acceptance surface — so this script always exits 0 so a native build
# failure never fails the release-candidate job itself; only its reported
# STATUS reflects the outcome.
#
# Requires: gh (authenticated via GITHUB_TOKEN in the environment), run from
# inside the project's checked-out repository, CANDIDATE_SHA set in the
# environment.
set -u

if [[ ! -f .github/workflows/build-desktop.yml ]]; then
  echo "STATUS=skipped"
  echo "URL="
  exit 0
fi

gh workflow run build-desktop.yml -f "sha=${CANDIDATE_SHA}" >/dev/null 2>&1

RUN_ID=""
for _ in $(seq 1 12); do
  sleep 5
  RUN_ID=$(gh run list --workflow=build-desktop.yml --json databaseId,headSha \
    --jq ".[] | select(.headSha==\"${CANDIDATE_SHA}\") | .databaseId" 2>/dev/null | head -n1)
  [[ -n "$RUN_ID" ]] && break
done

if [[ -z "$RUN_ID" ]]; then
  echo "STATUS=unknown"
  echo "URL="
  exit 0
fi

if gh run watch "$RUN_ID" --exit-status >/dev/null 2>&1; then
  STATUS="passed"
else
  STATUS="failed"
fi

URL=$(gh run view "$RUN_ID" --json url --jq ".url" 2>/dev/null)
echo "STATUS=${STATUS}"
echo "URL=${URL}"
