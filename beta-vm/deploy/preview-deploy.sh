#!/usr/bin/env bash
# preview-deploy.sh <project> <sha> <issue-key>
#
# Stands up an isolated, disposable preview container for a release
# candidate, reusing the image deploy.sh already built for this SHA — no
# rebuild. Labelled for Traefik routing (rc-<sha>.<PREVIEW_DOMAIN>) and with
# the Release ticket's key so preview-teardown-by-issue.sh can find it later
# even without knowing the SHA (e.g. a Release abandoned before its own
# comment recorded the SHA anywhere ScrumMaster can see).

set -euo pipefail
PROJECT="$1"
SHA="$2"
ISSUE_KEY="$3"

: "${PREVIEW_DOMAIN:?PREVIEW_DOMAIN must be set in the beta-deploy environment}"

IMAGE="beta-$PROJECT:$SHA"
CONTAINER="preview-$PROJECT-$SHA"
HOST="rc-$SHA.$PREVIEW_DOMAIN"

docker image inspect "$IMAGE" > /dev/null 2>&1 || {
  echo "No image $IMAGE found — deploy.sh must build it first for this SHA." >&2
  exit 1
}

docker rm -f "$CONTAINER" > /dev/null 2>&1 || true
docker run -d --name "$CONTAINER" \
  --network beta \
  --env-file "/home/beta-deploy/secrets/$PROJECT.env" \
  --label preview=true \
  --label "issue=$ISSUE_KEY" \
  --label "traefik.enable=true" \
  --label "traefik.http.routers.$CONTAINER.rule=Host(\`$HOST\`)" \
  --label "traefik.http.services.$CONTAINER.loadbalancer.server.port=8080" \
  "$IMAGE"

echo "Preview live: https://$HOST"
