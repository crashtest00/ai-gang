#!/usr/bin/env bash
# spike-cloudflare-tunnel-api.sh
#
# One-shot spike for token-based tunnel creation. NOT part of the
# production setup path — do not wire this into
# scripts/setup-cloudflare-tunnel.sh yet.
#
# RESOLVED 2026-09-06 against a real account: token-based creation works with
# config_src="local" (a locally-managed tunnel — the same kind
# `cloudflared tunnel login` produces). config_src="cloudflare"
# (remotely-managed, lives in the Zero Trust/One dashboard) fails with a
# generic "Authentication error" (code 10000) regardless of token
# permissions added — that failure is specific to the remotely-managed
# tunnel type, not a token/permission gap, and is not something this
# feature needs to solve since "local" already satisfies the no-browser-
# login-step requirement.
#
# What this script does, against a real Cloudflare account:
#   1. Creates a throwaway tunnel via POST /accounts/{account_id}/cfd_tunnel
#   2. Fetches its connector token via GET .../cfd_tunnel/{id}/token
#   3. Deletes the throwaway tunnel again (cleanup=true), leaving no residue
#   4. Prints a clear PASS/FAIL plus Cloudflare's raw error, if any
#
# It never touches the real "ai-gang" tunnel or existing DNS/Access config.
#
# Prerequisites:
#   Set in ~/ai-gang/.env (or export before running):
#     CF_API_KEY=<an API token scoped to the target account, with
#                 "Cloudflare Tunnel" Edit permission and that account
#                 selected under Account Resources>
#     CF_ACCOUNT_ID=<Account ID from Cloudflare dashboard -> right sidebar>
#
# Usage:
#   ./scripts/spike-cloudflare-tunnel-api.sh

set -euo pipefail

HQ_ENV="${HQ_ENV:-$HOME/ai-gang/.env}"
TEST_TUNNEL_NAME="ai-gang-spike-test-$$"
API="https://api.cloudflare.com/client/v4"

if [[ -f "$HQ_ENV" ]]; then
  # shellcheck source=/dev/null
  source "$HQ_ENV"
fi

: "${CF_API_KEY:?CF_API_KEY is not set. Check $HQ_ENV or export it directly.}"
: "${CF_ACCOUNT_ID:?CF_ACCOUNT_ID is not set. Check $HQ_ENV or export it directly.}"

auth_curl() {
  curl -s -H "Authorization: Bearer $CF_API_KEY" -H "Content-Type: application/json" "$@"
}

fail() {
  echo ""
  echo "RESULT: FAIL — $1"
  echo ""
  echo "Raw Cloudflare response:"
  echo "$2" | jq . 2>/dev/null || echo "$2"
  echo ""
  echo "If this is a permission error, add Account > Cloudflare Tunnel > Edit"
  echo "to the token being tested (Cloudflare dashboard -> My Profile ->"
  echo "API Tokens -> edit the token) and re-run this script."
  exit 1
}

echo "Spiking account-scoped Tunnel API against account $CF_ACCOUNT_ID..."
echo "Test tunnel name: $TEST_TUNNEL_NAME"

# --- Step 1: create ---
# config_src="local" is required here — "cloudflare" (remotely-managed)
# fails on this account regardless of token permissions; see the header
# comment above.
echo ""
echo "Creating tunnel via POST /accounts/{account_id}/cfd_tunnel..."
CREATE_RESULT=$(auth_curl -X POST "$API/accounts/$CF_ACCOUNT_ID/cfd_tunnel" \
  -d "$(jq -n --arg name "$TEST_TUNNEL_NAME" '{name: $name, config_src: "local"}')")

if ! echo "$CREATE_RESULT" | jq -e '.success == true' > /dev/null 2>&1; then
  fail "tunnel creation failed" "$CREATE_RESULT"
fi

TUNNEL_ID=$(echo "$CREATE_RESULT" | jq -r '.result.id')
echo "  Created: $TUNNEL_ID"

cleanup() {
  if [[ -n "${TUNNEL_ID:-}" ]]; then
    echo ""
    echo "Cleaning up test tunnel $TUNNEL_ID..."
    DELETE_RESULT=$(auth_curl -X DELETE "$API/accounts/$CF_ACCOUNT_ID/cfd_tunnel/$TUNNEL_ID?cascade=true" || true)
    if echo "$DELETE_RESULT" | jq -e '.success == true' > /dev/null 2>&1; then
      echo "  Deleted."
    else
      echo "  WARNING: cleanup delete may have failed — check the dashboard for a"
      echo "  leftover tunnel named $TEST_TUNNEL_NAME."
      echo "$DELETE_RESULT" | jq . 2>/dev/null || echo "$DELETE_RESULT"
    fi
  fi
}
trap cleanup EXIT

# --- Step 2: fetch connector token ---
echo ""
echo "Fetching connector token via GET .../cfd_tunnel/{id}/token..."
TOKEN_RESULT=$(auth_curl "$API/accounts/$CF_ACCOUNT_ID/cfd_tunnel/$TUNNEL_ID/token")

if ! echo "$TOKEN_RESULT" | jq -e '.success == true' > /dev/null 2>&1; then
  fail "token fetch failed" "$TOKEN_RESULT"
fi

echo "  Token retrieved (not printed — treat it as a live tunnel-run credential)."

echo ""
echo "RESULT: PASS"
echo ""
echo "The token being tested is sufficient for account-scoped, locally-managed"
echo "tunnel creation (cfd_tunnel create + token fetch, config_src=local)."
