#!/usr/bin/env bash
# reconcile-agent-field.sh
#
# Syncs the Jira Agent single-select field's options with the canonical
# agent catalog (services/scrummaster/config/agents.json). Jira is a projection of
# the catalog, never the other way around — this script never reads Jira to
# decide what's valid, only to decide what needs to change.
# See the agent-assignment design REQ-06.
#
# What it does:
#   - Creates a Jira option (enabled) for every active catalog id that has
#     no corresponding option yet.
#   - Disables (never deletes) the Jira option for every id listed in the
#     catalog's `retiredAgents` — preserves the value so historical Jira
#     work referencing it stays interpretable, per REQ-06.
#   - Reports, without modifying, any Jira option whose value is neither an
#     active catalog id nor a retired one — its provenance is unknown, so a
#     human decides whether to add it to agents.json or retire it deliberately.
#
# Usage:
#   ./scripts/reconcile-agent-field.sh
#
# Reads Jira credentials from ~/ai-gang/.env (JIRA_URL, JIRA_EMAIL, JIRA_TOKEN).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
HQ_ENV="${HQ_ENV:-$HOME/ai-gang/.env}"
SM_ENV="$REPO_ROOT/services/scrummaster/.env"
CATALOG_PATH="$REPO_ROOT/services/scrummaster/config/agents.json"

if [[ -f "$HQ_ENV" ]]; then
  # shellcheck source=/dev/null
  source "$HQ_ENV"
fi

: "${JIRA_URL:?JIRA_URL is not set. Check $HQ_ENV}"
: "${JIRA_EMAIL:?JIRA_EMAIL is not set. Check $HQ_ENV}"
: "${JIRA_TOKEN:?JIRA_TOKEN is not set. Check $HQ_ENV}"

AGENT_FIELD_ID=$(grep -E '^JIRA_AGENT_FIELD_ID=customfield_' "$SM_ENV" 2>/dev/null | cut -d= -f2 || true)
: "${AGENT_FIELD_ID:?JIRA_AGENT_FIELD_ID not found in $SM_ENV — run scripts/create-jira-fields.sh first}"

API="$JIRA_URL/rest/api/3"
AUTH="$JIRA_EMAIL:$JIRA_TOKEN"

jira_get() {
  curl -s -u "$AUTH" -H "Accept: application/json" "$API$1"
}

jira_post() {
  local path="$1" body="$2"
  curl -s -u "$AUTH" -H "Content-Type: application/json" -H "Accept: application/json" \
    -X POST "$API$path" -d "$body"
}

jira_put() {
  local path="$1" body="$2"
  curl -s -u "$AUTH" -H "Content-Type: application/json" -H "Accept: application/json" \
    -X PUT "$API$path" -d "$body"
}

CONTEXT_ID=$(jira_get "/field/$AGENT_FIELD_ID/context" | jq -r '.values[0].id')
if [[ -z "$CONTEXT_ID" || "$CONTEXT_ID" == "null" ]]; then
  echo "Error: could not find a context for field $AGENT_FIELD_ID." >&2
  exit 1
fi

mapfile -t ACTIVE_IDS < <(jq -r '.agents[].id' "$CATALOG_PATH")
mapfile -t RETIRED_IDS < <(jq -r '.retiredAgents[]?.id' "$CATALOG_PATH")

EXISTING_OPTIONS_JSON=$(jira_get "/field/$AGENT_FIELD_ID/context/$CONTEXT_ID/option")

existing_option_id() {
  local value="$1"
  echo "$EXISTING_OPTIONS_JSON" | jq -r --arg v "$value" '.values[] | select(.value == $v) | .id' | head -1
}

existing_option_disabled() {
  local value="$1"
  echo "$EXISTING_OPTIONS_JSON" | jq -r --arg v "$value" '.values[] | select(.value == $v) | .disabled' | head -1
}

echo "Reconciling Agent field ($AGENT_FIELD_ID) options against $CATALOG_PATH..."
echo ""

# --- Create missing options for active catalog ids ---
TO_CREATE=()
for id in "${ACTIVE_IDS[@]}"; do
  existing_id=$(existing_option_id "$id")
  if [[ -z "$existing_id" ]]; then
    TO_CREATE+=("$id")
  elif [[ "$(existing_option_disabled "$id")" == "true" ]]; then
    echo "  Re-enabling active catalog id currently disabled in Jira: $id"
    opt_id=$(existing_option_id "$id")
    jira_put "/field/$AGENT_FIELD_ID/context/$CONTEXT_ID/option" \
      "{\"options\": [{\"id\": \"$opt_id\", \"disabled\": false}]}" > /dev/null
  fi
done

if [[ ${#TO_CREATE[@]} -gt 0 ]]; then
  options_json=""
  for id in "${TO_CREATE[@]}"; do
    options_json="${options_json}{\"value\": \"$id\"},"
  done
  options_json="[${options_json%,}]"
  jira_post "/field/$AGENT_FIELD_ID/context/$CONTEXT_ID/option" "{\"options\": $options_json}" > /dev/null
  echo "  Created missing options: ${TO_CREATE[*]}"
else
  echo "  No missing options — every active catalog id already has a Jira option."
fi

echo ""

# --- Disable options for retired catalog ids (never delete) ---
for id in "${RETIRED_IDS[@]}"; do
  [[ -z "$id" ]] && continue
  opt_id=$(existing_option_id "$id")
  if [[ -z "$opt_id" ]]; then
    echo "  Retired id \"$id\" has no Jira option — nothing to disable."
    continue
  fi
  if [[ "$(existing_option_disabled "$id")" == "true" ]]; then
    echo "  Retired id \"$id\" is already disabled — OK."
  else
    jira_put "/field/$AGENT_FIELD_ID/context/$CONTEXT_ID/option" \
      "{\"options\": [{\"id\": \"$opt_id\", \"disabled\": true}]}" > /dev/null
    echo "  Disabled retired id \"$id\" (value preserved for historical Jira work)."
  fi
done

echo ""

# --- Report options with no catalog or retired-list backing ---
ALL_KNOWN_IDS=("${ACTIVE_IDS[@]}" "${RETIRED_IDS[@]}")
UNEXPECTED=()
while IFS= read -r value; do
  [[ -z "$value" ]] && continue
  known=false
  for id in "${ALL_KNOWN_IDS[@]}"; do
    [[ "$value" == "$id" ]] && known=true && break
  done
  [[ "$known" == false ]] && UNEXPECTED+=("$value")
done < <(echo "$EXISTING_OPTIONS_JSON" | jq -r '.values[].value')

if [[ ${#UNEXPECTED[@]} -gt 0 ]]; then
  echo "  Unexpected Jira options (no matching catalog or retiredAgents entry) — review manually:"
  for v in "${UNEXPECTED[@]}"; do
    echo "    - $v"
  done
  echo "  Add to agents.json if still valid, or to retiredAgents if intentionally removed, then re-run."
else
  echo "  No unexpected options."
fi

echo ""
echo "Done."
