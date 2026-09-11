#!/usr/bin/env bash
# setup-cloudflare-tunnel.sh
#
# Sets up a Cloudflare Tunnel so Jira can reach ScrumMaster over HTTPS
# without exposing port 9000 to the internet.
#
# What this script does:
#   1. Installs cloudflared (if not already installed)
#   2. Creates a named tunnel "ai-gang" via Cloudflare's account-scoped
#      Tunnel REST API (idempotent) — see "Token-based tunnel creation"
#      below (the graph-process-engine design REQ-11)
#   3. Writes /etc/cloudflared/config.yml (HQ always; Jenkins, the preview
#      wildcard, and the beta-app wildcard if their subdomain vars are set)
#   4. Creates CNAME DNS record(s) via the Cloudflare API
#   5. Configures Cloudflare Access (email one-time-PIN) on the preview and/or
#      beta-app wildcards, for whichever of PREVIEW_SUBDOMAIN/BETA_DOMAIN is
#      set and CF_ACCOUNT_ID is available
#   6. Installs cloudflared as a systemd service and starts it
#   7. Updates HQ_URL (and JENKINS_URL/PREVIEW_DOMAIN/BETA_DOMAIN if
#      applicable) in ~/ai-gang/.env
#
# Safe to re-run: the config is regenerated from .env each time, preserving all
# ingress entries defined by the variables below.
#
# Token-based tunnel creation (REQ-11):
#   This script no longer requires the interactive `cloudflared tunnel
#   login` browser step. Instead it creates the tunnel directly via
#   `POST /accounts/{CF_ACCOUNT_ID}/cfd_tunnel`, authenticated by
#   CF_API_KEY, with `config_src: "local"` — a locally-managed tunnel, the
#   same kind `cloudflared tunnel login` + `cloudflared tunnel create`
#   already produced. `config_src: "local"` (not `"cloudflare"`, a
#   remotely-managed Zero Trust-dashboard tunnel) is required: spiked
#   against a real account (scripts/spike-cloudflare-tunnel-api.sh),
#   `"cloudflare"` fails with a generic authentication error regardless of
#   token permissions, while `"local"` succeeds with the existing token and
#   no Zero Trust onboarding — see graph-process-engine.md's Open Questions
#   for the full writeup.
#
#   A locally-managed tunnel's credentials file
#   (~/.cloudflared/<tunnel_id>.json, cloudflared's own format:
#   {AccountTag, TunnelSecret, TunnelID}) can't be fetched from Cloudflare —
#   the API requires the *caller* to generate `tunnel_secret` (>=32 random
#   bytes, base64) and pass it in the create request; Cloudflare never
#   returns a secret it didn't receive. This script generates that secret
#   locally (`openssl rand -base64 32`) and writes the credentials file
#   itself immediately after a successful create call. Everything
#   downstream (config.yml's `credentials-file` field, the systemd service)
#   is unchanged from the CLI-driven flow this replaces.
#
#   Provisioning a second project is calling this script again with a
#   different CF_API_KEY/domain pair, not a second browser login.
#
# Prerequisites (must be done before running this script):
#   Set the following in ~/ai-gang/.env:
#        CF_API_KEY=<API token — needs "Cloudflare Tunnel" Edit permission
#                    (Account resources) to create the tunnel, "Edit zone
#                    DNS" template permissions (scoped to your zone) for the
#                    CNAME record(s), PLUS Account > Access: Apps and
#                    Policies > Edit if you will set PREVIEW_SUBDOMAIN or
#                    BETA_DOMAIN below. Missing the Access permission alone
#                    doesn't fail this script — configure_access() below
#                    falls back to a manual-setup warning>
#        CF_ZONE_ID=<Zone ID from Cloudflare dashboard → domain overview → right sidebar>
#        CF_ACCOUNT_ID=<Account ID from Cloudflare dashboard → right sidebar>  # required —
#                                                    # the account-scoped Tunnel API needs it
#                                                    # for every call, not only Access
#        HQ_SUBDOMAIN=hq.yourdomain.com
#        JENKINS_SUBDOMAIN=jenkins.yourdomain.com   # optional — omit if not using Jenkins
#        PREVIEW_SUBDOMAIN=*.preview.yourdomain.com # optional — release-candidate previews
#                                                    # (see the release-workflow design).
#                                                    # Routes to Traefik on the Beta VM, which
#                                                    # picks per-container routes from Docker
#                                                    # labels — this hostname is registered once.
#        BETA_DOMAIN=*.beta.yourdomain.com          # optional — the long-lived per-project Beta
#                                                    # app (beta-vm/deploy/deploy.sh), addressed as
#                                                    # <project>.<BETA_DOMAIN minus the leading *.>.
#                                                    # Also routed to Traefik on the Beta VM, same
#                                                    # as PREVIEW_SUBDOMAIN — one Traefik in front
#                                                    # of both previews and long-lived apps means
#                                                    # only one port needs to be network-reachable.
#        BETA_VM_HOST=<Beta VM's LAN/VPC IP or hostname>  # required if PREVIEW_SUBDOMAIN or
#                                                    # BETA_DOMAIN is set — this script normally
#                                                    # runs on the Dev VM, so Traefik's port must be
#                                                    # reached over the network, not localhost. Same
#                                                    # host Jenkins already uses for the beta-deploy
#                                                    # SSH path — put the two VMs on a shared
#                                                    # LAN/VPC and firewall this port to only accept
#                                                    # connections from the Dev VM (see beta-vm/README.md).
#        BETA_VM_TRAEFIK_PORT=8181                  # optional — defaults to 8181 if unset
#
# The branch points this script's manual prerequisites used to leave as
# silent dead ends (token/account missing, tunnel already existing, which
# subdomain vars are set) are now also modeled as a graph, walkable without
# running this script at all:
# setup/graphs/cloudflare-setup.graph.yaml (graph-process-engine.md REQ-10).
#
# Usage:
#   ./scripts/setup-cloudflare-tunnel.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
HQ_ENV="${HQ_ENV:-$HOME/ai-gang/.env}"
TUNNEL_NAME="ai-gang"
CF_CONFIG="/etc/cloudflared/config.yml"

# --- Load env ---
if [[ -f "$HQ_ENV" ]]; then
  # shellcheck source=/dev/null
  source "$HQ_ENV"
fi

: "${CF_API_KEY:?CF_API_KEY is not set. Check $HQ_ENV}"
: "${CF_ZONE_ID:?CF_ZONE_ID is not set. Check $HQ_ENV}"
: "${CF_ACCOUNT_ID:?CF_ACCOUNT_ID is not set. Check $HQ_ENV (Cloudflare dashboard -> right sidebar) — required for the account-scoped Tunnel API (REQ-11), not only Access}"
: "${HQ_SUBDOMAIN:?HQ_SUBDOMAIN is not set. Check $HQ_ENV (e.g. hq.yourdomain.com)}"
# Optional — include Jenkins ingress when set
JENKINS_SUBDOMAIN="${JENKINS_SUBDOMAIN:-}"
# Optional — release-candidate preview wildcard, routed to Traefik on the Beta VM
PREVIEW_SUBDOMAIN="${PREVIEW_SUBDOMAIN:-}"
# Optional — long-lived per-project Beta app wildcard, also routed to Traefik
BETA_DOMAIN="${BETA_DOMAIN:-}"
BETA_VM_TRAEFIK_PORT="${BETA_VM_TRAEFIK_PORT:-8181}"
BETA_VM_HOST="${BETA_VM_HOST:-}"
if [[ -n "$PREVIEW_SUBDOMAIN" || -n "$BETA_DOMAIN" ]]; then
  : "${BETA_VM_HOST:?BETA_VM_HOST is not set. Check $HQ_ENV (required when PREVIEW_SUBDOMAIN or BETA_DOMAIN is set — this script runs on the Dev VM, so Traefik on the Beta VM must be reached over the network, not localhost)}"
fi

CF_API="https://api.cloudflare.com/client/v4"
cf_curl() {
  curl -s -H "Authorization: Bearer $CF_API_KEY" -H "Content-Type: application/json" "$@"
}

# --- Step 1: Install cloudflared ---
# Still needed: cloudflared itself is what runs the tunnel connector as a
# systemd service (Step 6) — only *creating* the tunnel and authenticating
# to do so no longer needs it (REQ-11).
if command -v cloudflared &> /dev/null; then
  echo "cloudflared already installed: $(cloudflared --version 2>&1 | head -1)"
else
  echo "Installing cloudflared..."
  wget -q https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb \
    -O /tmp/cloudflared.deb
  sudo dpkg -i /tmp/cloudflared.deb
  rm /tmp/cloudflared.deb
  echo "  Installed: $(cloudflared --version 2>&1 | head -1)"
fi

# --- Step 2/3: Create tunnel via the account-scoped Tunnel API (idempotent) ---
# REQ-11: no `cloudflared tunnel login` browser step — CF_API_KEY plus
# CF_ACCOUNT_ID (both environment-supplied) and network access to the
# Cloudflare API are sufficient. See setup/graphs/cloudflare-setup.graph.yaml
# for this as a walkable decision node instead of this script's own
# idempotency check below.
echo ""
echo "Checking tunnel '$TUNNEL_NAME' via the Cloudflare API..."

LIST_RESULT=$(cf_curl "$CF_API/accounts/$CF_ACCOUNT_ID/cfd_tunnel?name=$TUNNEL_NAME&is_deleted=false")
if ! echo "$LIST_RESULT" | jq -e '.success == true' > /dev/null 2>&1; then
  echo "ERROR: Tunnel list API call failed:"
  echo "$LIST_RESULT" | jq . 2>/dev/null || echo "$LIST_RESULT"
  exit 1
fi
EXISTING=$(echo "$LIST_RESULT" | jq -r '.result[0].id // empty')

CREDS_FILE_DIR="$HOME/.cloudflared"
mkdir -p "$CREDS_FILE_DIR"

if [[ -n "$EXISTING" ]]; then
  TUNNEL_UUID="$EXISTING"
  echo "  Tunnel already exists: $TUNNEL_UUID"
  CREDS_FILE="$CREDS_FILE_DIR/${TUNNEL_UUID}.json"
  if [[ ! -f "$CREDS_FILE" ]]; then
    echo "ERROR: Tunnel '$TUNNEL_NAME' ($TUNNEL_UUID) already exists in this Cloudflare"
    echo "account, but its credentials file is missing locally: $CREDS_FILE"
    echo "This happens when re-running this script on a different machine, or after"
    echo "losing local state. Delete the tunnel and re-run to recreate it:"
    echo "  curl -X DELETE -H \"Authorization: Bearer \$CF_API_KEY\" \\"
    echo "    \"$CF_API/accounts/$CF_ACCOUNT_ID/cfd_tunnel/$TUNNEL_UUID?cascade=true\""
    exit 1
  fi
else
  echo "  Creating tunnel '$TUNNEL_NAME' via POST /accounts/{account_id}/cfd_tunnel..."
  # A locally-managed tunnel's credentials file can't be fetched from
  # Cloudflare — it's derived from a secret only the caller knows.
  # tunnel_secret must be >=32 random bytes, base64-encoded (Cloudflare API
  # requirement); we generate it here and it never leaves this machine
  # except inside the (TLS-protected) create request itself.
  TUNNEL_SECRET=$(openssl rand -base64 32)
  CREATE_RESULT=$(cf_curl -X POST "$CF_API/accounts/$CF_ACCOUNT_ID/cfd_tunnel" \
    -d "$(jq -n --arg name "$TUNNEL_NAME" --arg secret "$TUNNEL_SECRET" \
      '{name: $name, config_src: "local", tunnel_secret: $secret}')")
  if ! echo "$CREATE_RESULT" | jq -e '.success == true' > /dev/null 2>&1; then
    echo "ERROR: Tunnel creation failed:"
    echo "$CREATE_RESULT" | jq . 2>/dev/null || echo "$CREATE_RESULT"
    exit 1
  fi
  TUNNEL_UUID=$(echo "$CREATE_RESULT" | jq -r '.result.id')
  echo "  Created: $TUNNEL_UUID"

  # cloudflared's own locally-managed credentials-file format:
  # {AccountTag, TunnelSecret, TunnelID}. Writing this ourselves (instead of
  # `cloudflared tunnel create` writing it after an interactive login) is
  # the whole rewrite — everything downstream (config.yml's
  # credentials-file field, the systemd service) reads this file exactly as
  # before.
  CREDS_FILE="$CREDS_FILE_DIR/${TUNNEL_UUID}.json"
  jq -n --arg account "$CF_ACCOUNT_ID" --arg secret "$TUNNEL_SECRET" --arg id "$TUNNEL_UUID" \
    '{AccountTag: $account, TunnelSecret: $secret, TunnelID: $id}' > "$CREDS_FILE"
  chmod 600 "$CREDS_FILE"
  echo "  Wrote credentials file: $CREDS_FILE"
fi

# --- Step 4: Write config.yml ---
echo ""
echo "Writing $CF_CONFIG..."
sudo mkdir -p "$(dirname "$CF_CONFIG")"

JENKINS_INGRESS=""
if [[ -n "$JENKINS_SUBDOMAIN" ]]; then
  JENKINS_INGRESS="  - hostname: \"${JENKINS_SUBDOMAIN}\"
    service: http://localhost:8080
"
fi

# Preview and beta-app wildcards both point at the same Traefik instance on
# the Beta VM — Traefik disambiguates by Host header using the per-container
# Docker labels deploy.sh/preview-deploy.sh set, so no config file edits or
# tunnel restarts per release or per deploy. This script normally runs on
# the Dev VM, so the origin must be BETA_VM_HOST, not localhost — Traefik's
# port isn't on this machine. See release-workflow.md and beta-vm/README.md.
#
# Hostnames are quoted: a leading "*" (both wildcards start with one) is a
# YAML alias character unquoted, and would fail to parse otherwise.
PREVIEW_INGRESS=""
if [[ -n "$PREVIEW_SUBDOMAIN" ]]; then
  PREVIEW_INGRESS="  - hostname: \"${PREVIEW_SUBDOMAIN}\"
    service: http://${BETA_VM_HOST}:${BETA_VM_TRAEFIK_PORT}
"
fi

BETA_INGRESS=""
if [[ -n "$BETA_DOMAIN" ]]; then
  BETA_INGRESS="  - hostname: \"${BETA_DOMAIN}\"
    service: http://${BETA_VM_HOST}:${BETA_VM_TRAEFIK_PORT}
"
fi

sudo tee "$CF_CONFIG" > /dev/null <<CONFIG
tunnel: ${TUNNEL_UUID}
credentials-file: ${CREDS_FILE}

ingress:
  - hostname: "${HQ_SUBDOMAIN}"
    service: http://localhost:9000
${JENKINS_INGRESS}${PREVIEW_INGRESS}${BETA_INGRESS}  - service: http_status:404
CONFIG
echo "  Done."

# --- Step 5: Create CNAME DNS records via Cloudflare API ---
create_cname() {
  local hostname="$1"
  echo ""
  echo "Configuring DNS ($hostname → ${TUNNEL_UUID}.cfargotunnel.com)..."
  local existing
  existing=$(curl -s \
    -H "Authorization: Bearer $CF_API_KEY" \
    -H "Content-Type: application/json" \
    "https://api.cloudflare.com/client/v4/zones/$CF_ZONE_ID/dns_records?type=CNAME&name=$hostname" \
    | jq -r '.result[0].id // empty')
  if [[ -n "$existing" ]]; then
    echo "  CNAME record already exists — skipping."
    return
  fi
  local result
  # Cloudflare accepts a fully-qualified name here (not just the label
  # relative to the zone root) — needed for multi-label hosts like
  # *.preview.yourdomain.com or *.beta.yourdomain.com, where taking just the
  # first label would wildcard the zone apex instead.
  result=$(curl -s \
    -H "Authorization: Bearer $CF_API_KEY" \
    -H "Content-Type: application/json" \
    -X POST "https://api.cloudflare.com/client/v4/zones/$CF_ZONE_ID/dns_records" \
    -d "$(jq -n \
      --arg name "$hostname" \
      --arg content "${TUNNEL_UUID}.cfargotunnel.com" \
      '{type:"CNAME", name:$name, content:$content, proxied:true, ttl:1}')")
  if echo "$result" | jq -e '.success == true' > /dev/null 2>&1; then
    echo "  Created CNAME: $hostname → ${TUNNEL_UUID}.cfargotunnel.com"
  else
    echo "  ERROR: DNS record creation failed:"
    echo "$result" | jq .
    exit 1
  fi
}

create_cname "$HQ_SUBDOMAIN"
[[ -n "$JENKINS_SUBDOMAIN" ]] && create_cname "$JENKINS_SUBDOMAIN"
[[ -n "$PREVIEW_SUBDOMAIN" ]] && create_cname "$PREVIEW_SUBDOMAIN"
[[ -n "$BETA_DOMAIN" ]] && create_cname "$BETA_DOMAIN"

# --- Step 5: Configure Cloudflare Access on the preview/beta-app wildcards ---
# Gates the given domain specifically — not the HQ hostname, not Jenkins, not
# production. A login screen entirely outside the application, defaulting to
# email one-time-PIN, so an app with no auth of its own doesn't need one
# added just to get to a private preview or beta build. See
# release-strategy.md ("Beta is exposed to authorized testers").
configure_access() {
  local domain="$1" app_name="$2"

  if [[ -z "${CF_ACCOUNT_ID:-}" ]]; then
    echo ""
    echo "  CF_ACCOUNT_ID not set — skipping Cloudflare Access configuration."
    echo "  Configure manually: Zero Trust dashboard → Access → Applications →"
    echo "  Add an application → Self-hosted → domain: $domain →"
    echo "  identity provider: One-time PIN (email)."
    return
  fi

  echo ""
  echo "Configuring Cloudflare Access on $domain..."

  local existing
  existing=$(curl -s     -H "Authorization: Bearer $CF_API_KEY"     -H "Content-Type: application/json"     "https://api.cloudflare.com/client/v4/accounts/$CF_ACCOUNT_ID/access/apps"     | jq -r --arg dom "$domain" '.result[]? | select(.domain == $dom) | .id')

  if [[ -n "$existing" ]]; then
    echo "  Access application already exists — skipping."
    return
  fi

  local result
  result=$(curl -s     -H "Authorization: Bearer $CF_API_KEY"     -H "Content-Type: application/json"     -X POST "https://api.cloudflare.com/client/v4/accounts/$CF_ACCOUNT_ID/access/apps"     -d "$(jq -n       --arg name "$app_name"       --arg domain "$domain"       '{name:$name, domain:$domain, type:"self_hosted", session_duration:"24h"}')")

  if echo "$result" | jq -e '.success == true' > /dev/null 2>&1; then
    echo "  Access application created for $domain."
    echo "  NOTE: add a policy allowing the intended reviewers, and confirm the"
    echo "  One-time PIN (email) identity provider is enabled for this account —"
    echo "  both require a one-time Zero Trust dashboard step not covered by this API call."
  else
    echo "  Warning: Access application creation failed:"
    echo "$result" | jq .
    echo "  Configure manually via the Zero Trust dashboard (see above)."
  fi
}

[[ -n "$PREVIEW_SUBDOMAIN" ]] && configure_access "$PREVIEW_SUBDOMAIN" "AI Gang Release Previews"
[[ -n "$BETA_DOMAIN" ]] && configure_access "$BETA_DOMAIN" "AI Gang Beta"

# --- Step 6: Install and start systemd service ---
echo ""
echo "Installing cloudflared systemd service..."
sudo cloudflared service install 2>&1 || true  # already installed is fine
sudo systemctl enable cloudflared
sudo systemctl restart cloudflared
echo "  Service started."

# Wait a moment and check status
sleep 3
if ! sudo systemctl is-active --quiet cloudflared; then
  echo "  WARNING: cloudflared service does not appear to be running."
  echo "  Check: sudo systemctl status cloudflared"
else
  echo "  cloudflared is running."
fi

# --- Step 7: Update HQ_URL (and JENKINS_URL if set) in .env ---
echo ""
echo "Updating URLs in $HQ_ENV..."
NEW_HQ_URL="https://$HQ_SUBDOMAIN"
if grep -q '^HQ_URL=' "$HQ_ENV"; then
  sed -i "s|^HQ_URL=.*|HQ_URL=$NEW_HQ_URL|" "$HQ_ENV"
else
  printf '\nHQ_URL=%s\n' "$NEW_HQ_URL" >> "$HQ_ENV"
fi
echo "  HQ_URL=$NEW_HQ_URL"

if [[ -n "$JENKINS_SUBDOMAIN" ]]; then
  NEW_JENKINS_URL="https://$JENKINS_SUBDOMAIN"
  if grep -q '^JENKINS_URL=' "$HQ_ENV"; then
    sed -i "s|^JENKINS_URL=.*|JENKINS_URL=$NEW_JENKINS_URL|" "$HQ_ENV"
  else
    printf '\nJENKINS_URL=%s\n' "$NEW_JENKINS_URL" >> "$HQ_ENV"
  fi
  echo "  JENKINS_URL=$NEW_JENKINS_URL"
fi

if [[ -n "$PREVIEW_SUBDOMAIN" ]]; then
  # Strip the leading "*." — PREVIEW_DOMAIN is the bare domain the
  # release-candidate Jenkins job prefixes with rc-<sha>.
  NEW_PREVIEW_DOMAIN="${PREVIEW_SUBDOMAIN#\*.}"
  if grep -q '^PREVIEW_DOMAIN=' "$HQ_ENV"; then
    sed -i "s|^PREVIEW_DOMAIN=.*|PREVIEW_DOMAIN=$NEW_PREVIEW_DOMAIN|" "$HQ_ENV"
  else
    printf '\nPREVIEW_DOMAIN=%s\n' "$NEW_PREVIEW_DOMAIN" >> "$HQ_ENV"
  fi
  echo "  PREVIEW_DOMAIN=$NEW_PREVIEW_DOMAIN"
fi

if [[ -n "$BETA_DOMAIN" ]]; then
  # Strip the leading "*." — BETA_DOMAIN (bare) is what deploy.sh combines
  # with $PROJECT to build each project's stable Beta URL.
  NEW_BETA_DOMAIN="${BETA_DOMAIN#\*.}"
  if grep -q '^BETA_DOMAIN=' "$HQ_ENV"; then
    sed -i "s|^BETA_DOMAIN=.*|BETA_DOMAIN=$NEW_BETA_DOMAIN|" "$HQ_ENV"
  else
    printf '\nBETA_DOMAIN=%s\n' "$NEW_BETA_DOMAIN" >> "$HQ_ENV"
  fi
  echo "  BETA_DOMAIN=$NEW_BETA_DOMAIN"
fi

# --- Done ---
echo ""
echo "Done. Tunnel is live."
echo ""
echo "  HQ:      https://$HQ_SUBDOMAIN"
[[ -n "$JENKINS_SUBDOMAIN" ]] && echo "  Jenkins: https://$JENKINS_SUBDOMAIN"
echo ""
echo "Next steps:"
echo "  1. Verify: curl https://$HQ_SUBDOMAIN/health"
echo "  2. Run:    cd ~/ai-gang && ./scripts/init-project.sh"
echo "             (will auto-register the Jira webhook using the new HQ_URL)"
echo ""
