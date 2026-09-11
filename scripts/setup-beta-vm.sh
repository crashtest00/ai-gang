#!/usr/bin/env bash
# setup-beta-vm.sh
#
# Bootstraps the Beta VM's one-time infrastructure so a human never has to
# manually SSH in and follow beta-vm/README.md's setup steps by hand: the
# unprivileged beta-deploy account, rootless Docker, the Jenkins-side SSH
# keypair with its command= forced-command restriction, the deploy/ scripts
# and Traefik config, the `beta` Docker network Traefik's compose file
# expects to already exist, and the firewall rule limiting Traefik's port to
# the Dev VM.
#
# What this script does (run from the Dev VM):
#   1. Generates a dedicated ed25519 keypair for Jenkins -> beta-deploy SSH
#      (skipped if one already exists)
#   2. Determines this VM's LAN/VPC source IP as seen by the Beta VM, for
#      the firewall allowlist
#   3. SSHes into the Beta VM as an existing sudo-capable admin user and:
#      - creates the beta-deploy system account (idempotent)
#      - installs Docker + configures rootless Docker for beta-deploy,
#        enabling lingering so it survives without an active login session
#      - creates the `beta` Docker network (beta-vm/traefik/docker-compose.yml
#        declares it `external: true` — it is never created by `compose up`
#        itself)
#      - creates /home/beta-deploy/{repos,secrets} for deploy.sh's
#        per-project checkouts and env files
#      - installs the generated public key into
#        ~beta-deploy/.ssh/authorized_keys with the command= forced-command
#        restriction from deploy/authorized_keys.example
#      - writes /opt/beta-deploy/env (PREVIEW_DOMAIN/BETA_DOMAIN, mode 600)
#      - adds a ufw rule allowing the Dev VM's IP on BETA_VM_TRAEFIK_PORT,
#        if ufw is present
#   4. Copies beta-vm/deploy/*.sh and beta-vm/traefik/docker-compose.yml to
#      the Beta VM, owned by and executable only by beta-deploy
#   5. Starts Traefik under beta-deploy's rootless Docker context
#
# What this script deliberately does NOT do (still separate steps):
#   - The read-only GitHub deploy key (beta-vm/README.md step 3) is
#     per-project, not per-VM — it belongs with each project's Phase 3
#     setup (init-project.sh), not a one-time Beta VM bootstrap.
#   - Registering the generated private key as a Jenkins credential — see
#     setup/JenkinsConfig.md's "Deploy credentials will be added here once
#     deployment targets are decided" TODO. This script prints the private
#     key's path; add it to Jenkins as an "SSH Username with private key"
#     credential once that TODO is picked up.
#   - Provisioning the Beta VM itself, or a Prod VM equivalent — see
#     beta-vm/README.md's own scope note. A Prod version of this script
#     would need its own pass once Prod's deploy path is fully specified.
#
# Assumes a Debian/Ubuntu Beta VM (apt, ufw, systemd-logind) — the same
# assumption setup-cloudflare-tunnel.sh makes for the Dev VM.
#
# Prerequisites (must be done before running this script):
#   a) You already have SSH access to the Beta VM as a sudo-capable user —
#      this script reuses that access once, the same way
#      setup-cloudflare-tunnel.sh reuses a one-time `cloudflared tunnel
#      login` browser step. It never needs your personal key to persist on
#      the Beta VM afterward.
#   b) Set the following in ~/ai-gang/.env:
#        BETA_VM_HOST=<Beta VM's LAN/VPC IP or hostname>   # same var
#                                                  # setup-cloudflare-tunnel.sh uses
#        BETA_VM_ADMIN_USER=<your existing sudo-capable SSH user on that box>
#        BETA_VM_TRAEFIK_PORT=8181                # optional, defaults to 8181
#        PREVIEW_DOMAIN=preview.yourdomain.com    # bare domain, written by
#                                                  # setup-cloudflare-tunnel.sh
#        BETA_DOMAIN=beta.yourdomain.com          # bare domain, written by
#                                                  # setup-cloudflare-tunnel.sh
#      At least one of PREVIEW_DOMAIN/BETA_DOMAIN must be set — deploy/env
#      needs them.
#
# Safe to re-run: every remote step checks for existing state first.
#
# Usage:
#   ./scripts/setup-beta-vm.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
HQ_ENV="${HQ_ENV:-$HOME/ai-gang/.env}"
DEPLOY_SRC="$REPO_ROOT/beta-vm/deploy"
TRAEFIK_SRC="$REPO_ROOT/beta-vm/traefik/docker-compose.yml"
DEPLOY_KEY="${DEPLOY_KEY:-$HOME/.ssh/beta-deploy-jenkins}"

# --- Load env ---
if [[ -f "$HQ_ENV" ]]; then
  # shellcheck source=/dev/null
  source "$HQ_ENV"
fi

: "${BETA_VM_HOST:?BETA_VM_HOST is not set. Check $HQ_ENV}"
: "${BETA_VM_ADMIN_USER:?BETA_VM_ADMIN_USER is not set. Check $HQ_ENV (your existing sudo-capable SSH user on the Beta VM)}"
BETA_VM_TRAEFIK_PORT="${BETA_VM_TRAEFIK_PORT:-8181}"
PREVIEW_DOMAIN="${PREVIEW_DOMAIN:-}"
BETA_DOMAIN="${BETA_DOMAIN:-}"
if [[ -z "$PREVIEW_DOMAIN" && -z "$BETA_DOMAIN" ]]; then
  echo "ERROR: at least one of PREVIEW_DOMAIN or BETA_DOMAIN must be set in $HQ_ENV" >&2
  exit 1
fi

SSH_ADMIN="$BETA_VM_ADMIN_USER@$BETA_VM_HOST"

# --- Step 1: Generate the Jenkins -> beta-deploy keypair ---
echo ""
echo "Checking for Jenkins deploy keypair..."
if [[ -f "$DEPLOY_KEY" ]]; then
  echo "  Already exists: $DEPLOY_KEY"
else
  ssh-keygen -t ed25519 -N "" -C "jenkins-beta-deploy" -f "$DEPLOY_KEY" -q
  echo "  Generated: $DEPLOY_KEY"
fi
PUBKEY="$(cat "${DEPLOY_KEY}.pub")"

# --- Step 2: Determine the Dev VM's source IP as seen by the Beta VM ---
echo ""
echo "Determining this VM's LAN/VPC IP as seen reaching $BETA_VM_HOST..."
DEV_VM_SRC_IP="$(ip -o route get "$BETA_VM_HOST" 2>/dev/null | grep -oP '(?<=src )\S+' | head -1)"
if [[ -z "$DEV_VM_SRC_IP" ]]; then
  echo "ERROR: could not determine local source IP for $BETA_VM_HOST." >&2
  echo "Are the Dev and Beta VMs on the same LAN/VPC? (see beta-vm/README.md step 6)" >&2
  exit 1
fi
echo "  $DEV_VM_SRC_IP"

# --- Step 3: Remote bootstrap (account, rootless Docker, network, keys, env, firewall) ---
echo ""
echo "Bootstrapping beta-deploy on $BETA_VM_HOST..."
REMOTE_ENV=$(printf 'PUBKEY=%q DEV_VM_SRC_IP=%q BETA_VM_TRAEFIK_PORT=%q PREVIEW_DOMAIN=%q BETA_DOMAIN=%q' \
  "$PUBKEY" "$DEV_VM_SRC_IP" "$BETA_VM_TRAEFIK_PORT" "$PREVIEW_DOMAIN" "$BETA_DOMAIN")

# shellcheck disable=SC2087
ssh "$SSH_ADMIN" "sudo env $REMOTE_ENV bash -s" <<'REMOTE_SETUP'
set -euo pipefail

if ! id beta-deploy &>/dev/null; then
  useradd -m -s /bin/bash beta-deploy
  echo "Created beta-deploy account."
else
  echo "beta-deploy account already exists."
fi
loginctl enable-linger beta-deploy

if ! command -v docker &>/dev/null; then
  curl -fsSL https://get.docker.com | sh
fi
apt-get install -y -qq uidmap dbus-user-session docker-ce-rootless-extras >/dev/null 2>&1 || true

BETA_UID="$(id -u beta-deploy)"
if [[ ! -S "/run/user/${BETA_UID}/docker.sock" ]]; then
  su - beta-deploy -c "XDG_RUNTIME_DIR=/run/user/${BETA_UID} dockerd-rootless-setuptool.sh install"
fi

if ! grep -q DOCKER_HOST /home/beta-deploy/.bashrc 2>/dev/null; then
  cat >> /home/beta-deploy/.bashrc <<PROFILE
export XDG_RUNTIME_DIR=/run/user/${BETA_UID}
export PATH=/usr/bin:\$PATH
export DOCKER_HOST=unix:///run/user/${BETA_UID}/docker.sock
PROFILE
fi

su - beta-deploy -c 'source ~/.bashrc; docker network inspect beta >/dev/null 2>&1 || docker network create beta'

mkdir -p /home/beta-deploy/repos /home/beta-deploy/secrets
chown -R beta-deploy:beta-deploy /home/beta-deploy/repos /home/beta-deploy/secrets
chmod 700 /home/beta-deploy/repos /home/beta-deploy/secrets

mkdir -p /home/beta-deploy/.ssh
chmod 700 /home/beta-deploy/.ssh
touch /home/beta-deploy/.ssh/authorized_keys
if ! grep -qF "$PUBKEY" /home/beta-deploy/.ssh/authorized_keys; then
  echo "command=\"/opt/beta-deploy/forced-command.sh\",no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty $PUBKEY" \
    >> /home/beta-deploy/.ssh/authorized_keys
  echo "Installed forced-command key in authorized_keys."
else
  echo "Key already present in authorized_keys."
fi
chown beta-deploy:beta-deploy /home/beta-deploy/.ssh/authorized_keys
chmod 600 /home/beta-deploy/.ssh/authorized_keys

cat > /opt/beta-deploy-env.tmp <<ENVFILE
PREVIEW_DOMAIN=${PREVIEW_DOMAIN}
BETA_DOMAIN=${BETA_DOMAIN}
ENVFILE

if command -v ufw &>/dev/null; then
  ufw allow from "${DEV_VM_SRC_IP}" to any port "${BETA_VM_TRAEFIK_PORT}" proto tcp
  echo "ufw rule added: ${DEV_VM_SRC_IP} -> port ${BETA_VM_TRAEFIK_PORT}"
else
  echo "WARNING: ufw not found — firewall Traefik's port (${BETA_VM_TRAEFIK_PORT}) to ${DEV_VM_SRC_IP} manually."
fi

echo "Remote account/network/key bootstrap done."
REMOTE_SETUP

# --- Step 4: Copy deploy scripts + Traefik config, install env file ---
echo ""
echo "Copying deploy scripts and Traefik config to $BETA_VM_HOST..."
ssh "$SSH_ADMIN" 'mkdir -p /tmp/beta-deploy-staging/scripts /tmp/beta-deploy-staging/traefik'
scp -q "$DEPLOY_SRC"/deploy.sh "$DEPLOY_SRC"/preview-deploy.sh "$DEPLOY_SRC"/preview-teardown.sh \
  "$DEPLOY_SRC"/preview-teardown-by-issue.sh "$DEPLOY_SRC"/forced-command.sh \
  "$SSH_ADMIN:/tmp/beta-deploy-staging/scripts/"
scp -q "$TRAEFIK_SRC" "$SSH_ADMIN:/tmp/beta-deploy-staging/traefik/"

ssh "$SSH_ADMIN" 'sudo bash -s' <<'REMOTE_INSTALL'
set -euo pipefail
mkdir -p /opt/beta-deploy/traefik
mv /tmp/beta-deploy-staging/scripts/*.sh /opt/beta-deploy/
mv /tmp/beta-deploy-staging/traefik/docker-compose.yml /opt/beta-deploy/traefik/
mv /opt/beta-deploy-env.tmp /opt/beta-deploy/env
rm -rf /tmp/beta-deploy-staging
chown -R beta-deploy:beta-deploy /opt/beta-deploy
chmod 750 /opt/beta-deploy/*.sh
chmod 600 /opt/beta-deploy/env
echo "Deploy scripts, Traefik config, and env file installed."
REMOTE_INSTALL

# --- Step 5: Start Traefik under beta-deploy's rootless Docker ---
echo ""
echo "Starting Traefik..."
ssh "$SSH_ADMIN" 'sudo -u beta-deploy env XDG_RUNTIME_DIR=/run/user/$(id -u beta-deploy) DOCKER_HOST=unix:///run/user/$(id -u beta-deploy)/docker.sock bash -c "cd /opt/beta-deploy/traefik && docker compose up -d"'
echo "  Traefik started."

# --- Done ---
echo ""
echo "Done. Beta VM bootstrapped."
echo ""
echo "Still separate (not handled by this script):"
echo "  - Per-project read-only GitHub deploy key for beta-deploy (per repo, done in Phase 3)"
echo "  - Registering $DEPLOY_KEY as a Jenkins SSH credential (see setup/JenkinsConfig.md)"
echo ""
echo "Verify:"
echo "  curl http://$BETA_VM_HOST:$BETA_VM_TRAEFIK_PORT  # should reach Traefik (404 is expected — no routes yet)"
