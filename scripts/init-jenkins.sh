#!/usr/bin/env bash
# init-jenkins.sh
#
# One-time bootstrap for the AI Gang Jenkins master container.
#
# What it does:
#   1. Validates prerequisites (Docker, docker compose, jq, required env vars)
#   2. Ensures the ai-gang Docker network exists
#   3. Generates a random JENKINS_ADMIN_PASSWORD if not already set
#   4. Builds the Jenkins image (installs plugins at build time)
#   5. Starts the container (JCasC applies credentials and job config on first boot)
#   6. Waits for Jenkins to become healthy
#   7. Prints a summary and next steps
#
# Usage:
#   ./scripts/init-jenkins.sh
#
# Prerequisites:
#   - ~/ai-gang/.env contains:
#       JIRA_URL, JIRA_EMAIL, JIRA_TOKEN
#       GITHUB_TOKEN
#       JENKINS_URL      (optional — set after first boot, used by ScrumMaster)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
JENKINS_DIR="$REPO_ROOT/jenkins"
HQ_ENV="${HQ_ENV:-$HOME/ai-gang/.env}"

# ── Colours ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
ok()   { echo -e "${GREEN}  ✓${NC} $*"; }
warn() { echo -e "${YELLOW}  ⚠${NC}  $*"; }
fail() { echo -e "${RED}  ✗${NC} $*"; exit 1; }
step() { echo ""; echo "$*"; }

# ── Load .env ────────────────────────────────────────────────────────────────
if [[ -f "$HQ_ENV" ]]; then
  # shellcheck source=/dev/null
  set -o allexport; source "$HQ_ENV"; set +o allexport
fi

# ── Prerequisites ─────────────────────────────────────────────────────────────
step "Checking prerequisites..."

command -v docker  &>/dev/null || fail "docker is not installed."
command -v jq      &>/dev/null || fail "jq is not installed (apt install jq)."
docker compose version &>/dev/null || fail "docker compose plugin is not installed."
ok "docker, docker compose, jq"

[[ -d "$JENKINS_DIR" ]]            || fail "Jenkins directory not found: $JENKINS_DIR"
[[ -f "$JENKINS_DIR/Dockerfile" ]] || fail "Dockerfile not found in $JENKINS_DIR"
[[ -f "$JENKINS_DIR/plugins.txt" ]] || fail "plugins.txt not found in $JENKINS_DIR"
[[ -f "$JENKINS_DIR/jenkins.yaml" ]] || fail "jenkins.yaml not found in $JENKINS_DIR"
ok "Jenkins directory structure"

# Required secrets
MISSING_VARS=()
for var in JIRA_URL JIRA_EMAIL JIRA_TOKEN GITHUB_TOKEN; do
  [[ -z "${!var:-}" ]] && MISSING_VARS+=("$var")
done

if [[ ${#MISSING_VARS[@]} -gt 0 ]]; then
  echo ""
  echo -e "${RED}Missing required environment variables in $HQ_ENV:${NC}"
  for v in "${MISSING_VARS[@]}"; do echo "  $v"; done
  echo ""
  echo "Add them to $HQ_ENV and re-run."
  exit 1
fi
ok "Required env vars present"


# ── Docker network ────────────────────────────────────────────────────────────
step "Ensuring ai-gang Docker network..."
if docker network inspect ai-gang &>/dev/null; then
  ok "Network 'ai-gang' already exists."
else
  docker network create ai-gang
  ok "Created network 'ai-gang'."
fi

# ── Admin password ────────────────────────────────────────────────────────────
step "Jenkins admin password..."
if grep -q '^JENKINS_ADMIN_PASSWORD=.\+' "$HQ_ENV" 2>/dev/null; then
  ok "JENKINS_ADMIN_PASSWORD already set in $HQ_ENV."
  set -o allexport; source "$HQ_ENV"; set +o allexport
else
  JENKINS_ADMIN_PASSWORD=$(openssl rand -base64 24)
  if grep -q '^JENKINS_ADMIN_PASSWORD=' "$HQ_ENV" 2>/dev/null; then
    sed -i "s|^JENKINS_ADMIN_PASSWORD=.*|JENKINS_ADMIN_PASSWORD=$JENKINS_ADMIN_PASSWORD|" "$HQ_ENV"
  else
    printf '\nJENKINS_ADMIN_PASSWORD=%s\n' "$JENKINS_ADMIN_PASSWORD" >> "$HQ_ENV"
  fi
  ok "Generated JENKINS_ADMIN_PASSWORD and saved to $HQ_ENV."
fi

# ── JENKINS_URL ───────────────────────────────────────────────────────────────
step "Checking JENKINS_URL..."
if grep -q '^JENKINS_URL=.\+' "$HQ_ENV" 2>/dev/null; then
  JENKINS_URL="$(grep '^JENKINS_URL=' "$HQ_ENV" | cut -d= -f2)"
  ok "JENKINS_URL=$JENKINS_URL"
else
  DROPLET_IP=$(curl -s --max-time 3 http://169.254.169.254/metadata/v1/interfaces/public/0/ipv4/address 2>/dev/null || true)
  if [[ -n "$DROPLET_IP" ]]; then
    JENKINS_URL="http://${DROPLET_IP}:8080"
  else
    JENKINS_URL="http://localhost:8080"
  fi
  if grep -q '^JENKINS_URL=' "$HQ_ENV" 2>/dev/null; then
    sed -i "s|^JENKINS_URL=.*|JENKINS_URL=$JENKINS_URL|" "$HQ_ENV"
  else
    printf '\nJENKINS_URL=%s\n' "$JENKINS_URL" >> "$HQ_ENV"
  fi
  ok "Set JENKINS_URL=$JENKINS_URL in $HQ_ENV."
  [[ "$JENKINS_URL" == *"localhost"* ]] && \
    warn "JENKINS_URL is localhost — update $HQ_ENV to the public URL so GitHub webhooks work."
fi

# Export so docker compose picks them up
export JIRA_URL JIRA_EMAIL JIRA_TOKEN GITHUB_TOKEN JENKINS_ADMIN_PASSWORD JENKINS_URL

# ── Build ─────────────────────────────────────────────────────────────────────
step "Building Jenkins image (plugins install during build — takes a few minutes)..."
docker compose -f "$JENKINS_DIR/docker-compose.yml" build
ok "Image built."

# ── Start ─────────────────────────────────────────────────────────────────────
step "Starting Jenkins container..."

# Stop and remove if already running, so JCasC gets a clean apply
if docker ps -a --format '{{.Names}}' | grep -q '^ai-gang-jenkins$'; then
  warn "Existing ai-gang-jenkins container found — stopping and removing for clean config apply."
  docker rm -f ai-gang-jenkins
fi

docker compose -f "$JENKINS_DIR/docker-compose.yml" up -d
ok "Container started."

# ── Wait for Jenkins ──────────────────────────────────────────────────────────
step "Waiting for Jenkins to become ready (this takes ~60s on first boot)..."

JENKINS_LOCAL="http://localhost:8080"
MAX_WAIT=180
INTERVAL=5
elapsed=0

while true; do
  status=$(curl -s -o /dev/null -w "%{http_code}" "$JENKINS_LOCAL/login" 2>/dev/null || true)
  if [[ "$status" == "200" ]]; then
    ok "Jenkins is ready."
    break
  fi

  if [[ $elapsed -ge $MAX_WAIT ]]; then
    echo ""
    fail "Jenkins did not become ready within ${MAX_WAIT}s. Check logs: docker logs ai-gang-jenkins"
  fi

  echo -n "."
  sleep $INTERVAL
  elapsed=$((elapsed + INTERVAL))
done

# ── Verify Jira connection ─────────────────────────────────────────────────────
step "Verifying Jira connection via Jenkins API..."

CRUMB=$(curl -s -u "admin:${JENKINS_ADMIN_PASSWORD}" \
  "$JENKINS_LOCAL/crumbIssuer/api/json" | jq -r '.crumb // empty')

if [[ -z "$CRUMB" ]]; then
  warn "Could not retrieve Jenkins crumb — skipping Jira connection test."
  warn "Verify manually: Manage Jenkins → System → Jira → Test Connection."
else
  jira_test=$(curl -s -u "admin:${JENKINS_ADMIN_PASSWORD}" \
    -H "Jenkins-Crumb: $CRUMB" \
    -X POST "$JENKINS_LOCAL/descriptorByName/hudson.plugins.jira.JiraGlobalConfiguration/testConnection" \
    -d "url=${JIRA_URL}&credentialsId=jira-token" 2>/dev/null || true)

  if echo "$jira_test" | grep -qi "success\|ok"; then
    ok "Jira connection verified."
  else
    warn "Jira connection test inconclusive — verify manually."
    warn "Manage Jenkins → System → Jira → Test Connection."
  fi
fi

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════════════════"
echo " Jenkins is running"
echo "════════════════════════════════════════════════════════════"
echo ""
echo "  UI:       http://localhost:8080"
echo "  Login:    admin / (see JENKINS_ADMIN_PASSWORD in $HQ_ENV)"
echo "  URL:      $JENKINS_URL"
echo ""
echo "Next steps:"
echo ""
echo "  1. For each project, run init-project.sh — it registers the"
echo "     GitHub webhook pointing at Jenkins automatically."
echo ""
echo "  2. Run the smoke test in setup/JenkinsConfig.md § 9."
echo ""
