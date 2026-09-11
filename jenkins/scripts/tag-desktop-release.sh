#!/usr/bin/env bash
# Tags and pushes the next vX.Y.Z version at CANDIDATE_SHA when the current
# directory is a desktop-lane project (has .github/workflows/build-desktop.yml).
# The pushed tag is what triggers release-desktop.yml on GitHub, which builds
# and publishes the cross-platform GitHub Release. Prints:
#   TAG=<the tag just pushed, or empty if this is not a desktop-lane project>
# for the calling Jenkins pipeline stage to capture via `sh(returnStdout: true)`.
#
# Unlike trigger-native-build.sh, this step is REQUIRED production behavior for
# desktop projects (the desktop-app-support design REQ-07), not
# supplementary validation — `set -e` lets a real git/gh failure fail the
# production-promote job so it surfaces through the existing post-failure Jira
# comment, instead of silently skipping the release tag.
#
# Requires: git configured to push to origin, run from inside the project's
# checked-out repository, CANDIDATE_SHA set in the environment.
set -euo pipefail

if [[ ! -f .github/workflows/build-desktop.yml ]]; then
  echo "TAG="
  exit 0
fi

git fetch --tags origin

LAST_TAG=$(git tag --list 'v*' | sort -V | tail -n1)
if [[ -z "$LAST_TAG" ]]; then
  NEXT_TAG="v0.1.0"
else
  NEXT_TAG=$(echo "$LAST_TAG" | awk -F. '{ printf "v%d.%d.%d", substr($1,2)+0, $2+0, $3+1 }')
fi

git tag "$NEXT_TAG" "${CANDIDATE_SHA}"
git push origin "$NEXT_TAG"
echo "TAG=${NEXT_TAG}"
