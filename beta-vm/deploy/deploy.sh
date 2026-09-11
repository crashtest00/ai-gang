#!/usr/bin/env bash
# deploy.sh <project> <sha>
#
# Runs as the unprivileged beta-deploy account, under rootless Docker. Fetches
# and checks out the exact Jenkins-approved SHA using a read-only repo deploy
# key, builds the Beta image once, and restarts the long-lived Beta service.
# Called both for normal dev->beta deploys (per-project Jenkinsfile) and as
# the first step before a preview reuses this same image.
#
# Exposed at https://$PROJECT.$BETA_DOMAIN via Traefik (same label-based
# routing preview-deploy.sh uses) rather than a published host port — with
# multiple projects sharing this VM, a fixed host port would collide across
# them. Every project's app is expected to listen on 8080 internally, same
# convention as preview-deploy.sh.

set -euo pipefail
PROJECT="$1"
SHA="$2"

: "${BETA_DOMAIN:?BETA_DOMAIN must be set in the beta-deploy environment}"

REPO_DIR="/home/beta-deploy/repos/$PROJECT"
IMAGE="beta-$PROJECT:$SHA"
SECRETS_FILE="/home/beta-deploy/secrets/$PROJECT.env"
CONTAINER="beta-$PROJECT"
HOST="$PROJECT.$BETA_DOMAIN"

git -C "$REPO_DIR" fetch origin "$SHA"
git -C "$REPO_DIR" checkout --detach "$SHA"

# Build once — preview-deploy.sh reuses this exact tag without rebuilding.
docker build -t "$IMAGE" "$REPO_DIR"

docker rm -f "$CONTAINER" > /dev/null 2>&1 || true
docker run -d --name "$CONTAINER" \
  --network beta \
  --env-file "$SECRETS_FILE" \
  --restart unless-stopped \
  --label "traefik.enable=true" \
  --label "traefik.http.routers.$CONTAINER.rule=Host(\`$HOST\`)" \
  --label "traefik.http.services.$CONTAINER.loadbalancer.server.port=8080" \
  "$IMAGE"

echo "Deployed $PROJECT @ $SHA as $CONTAINER — https://$HOST"
