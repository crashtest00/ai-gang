#!/usr/bin/env bash
# create-jira-fields.sh
#
# Creates the Agent and Blocked custom fields on the Jira instance and
# writes their IDs directly into the platform .env (~/ai-gang/.env), where
# scripts/startup/derive-env.sh's JIRA_FIELD_ID_VARS loop reads them
# (canonical-delivery-state.md REQ-10).
#
# Idempotent: exits immediately if both IDs are already in the platform .env,
# and skips individual field creation if the field already exists in Jira.
#
# Usage:
#   ./scripts/create-jira-fields.sh
#
# Reads Jira credentials from ~/ai-gang/.env (JIRA_URL, JIRA_EMAIL, JIRA_TOKEN).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
HQ_ENV="${HQ_ENV:-$HOME/ai-gang/.env}"
# canonical-delivery-state.md REQ-10, "The field ids reach `core`": the ids
# are written into the platform .env that scripts/startup/derive-env.sh's
# JIRA_FIELD_ID_VARS loop reads, not into services/scrummaster/.env, which
# nothing Jira-facing has read since v5.1 moved the Jira client into
# Django/core. derive-env.sh carries them from here into services/core/.env,
# where the outbound writer and connect_jira read them (OQ-09; v5.1
# BUGFIXES BF-03).
FIELD_ID_ENV="$HQ_ENV"

# --- Load credentials ---
if [[ -f "$HQ_ENV" ]]; then
  # shellcheck source=/dev/null
  source "$HQ_ENV"
fi

: "${JIRA_URL:?JIRA_URL is not set. Check $HQ_ENV}"
: "${JIRA_EMAIL:?JIRA_EMAIL is not set. Check $HQ_ENV}"
: "${JIRA_TOKEN:?JIRA_TOKEN is not set. Check $HQ_ENV}"

API="$JIRA_URL/rest/api/3"
AUTH="$JIRA_EMAIL:$JIRA_TOKEN"

# --- Early exit if already configured ---
ALL_FIELD_VARS=(
  JIRA_AGENT_FIELD_ID
  JIRA_BLOCKED_FIELD_ID
  JIRA_VALUE_HYPOTHESIS_FIELD_ID
  JIRA_TEST_MEASUREMENT_FIELD_ID
  JIRA_BEHAVIOR_FIELD_ID
  JIRA_AC_FIELD_ID
  JIRA_CONSTRAINTS_FIELD_ID
  JIRA_EDGE_CASES_FIELD_ID
  JIRA_OUT_OF_SCOPE_FIELD_ID
)

if [[ -f "$FIELD_ID_ENV" ]]; then
  ALL_SET=true
  for var in "${ALL_FIELD_VARS[@]}"; do
    if ! grep -qE "^${var}=customfield_" "$FIELD_ID_ENV"; then
      ALL_SET=false
      break
    fi
  done
  if [[ "$ALL_SET" == true ]]; then
    echo "All field IDs already set in $FIELD_ID_ENV — nothing to do."
    for var in "${ALL_FIELD_VARS[@]}"; do
      val=$(grep "^${var}=" "$FIELD_ID_ENV" | cut -d= -f2)
      echo "  ${var}=${val}"
    done
    exit 0
  fi
fi

# --- Helpers ---

jira_get() {
  curl -s -u "$AUTH" -H "Accept: application/json" "$API$1"
}

jira_post() {
  local path="$1" body="$2"
  curl -s -u "$AUTH" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json" \
    -X POST "$API$path" \
    -d "$body"
}

get_existing_field_id() {
  local name="$1"
  jira_get "/field" | jq -r --arg name "$name" '.[] | select(.name == $name) | .id' | head -1
}

create_field() {
  local name="$1" type="$2"
  local response
  response=$(jira_post "/field" "{\"name\": \"$name\", \"type\": \"$type\"}")
  echo "$response" | jq -r '.id'
}

get_context_id() {
  local field_id="$1"
  jira_get "/field/$field_id/context" | jq -r '.values[0].id'
}

add_options() {
  local field_id="$1" context_id="$2"
  shift 2
  local options_json=""
  for opt in "$@"; do
    options_json="${options_json}{\"value\": \"$opt\"},"
  done
  options_json="[${options_json%,}]"
  jira_post "/field/$field_id/context/$context_id/option" "{\"options\": $options_json}" > /dev/null
}

# Write or update a KEY=VALUE line in the platform .env
write_env_var() {
  local key="$1" value="$2"
  if [[ -f "$FIELD_ID_ENV" ]] && grep -q "^${key}=" "$FIELD_ID_ENV"; then
    sed -i "s|^${key}=.*|${key}=${value}|" "$FIELD_ID_ENV"
  else
    echo "${key}=${value}" >> "$FIELD_ID_ENV"
  fi
}

# --- Main ---

echo "Checking Jira custom fields..."
echo ""

# Agent field — options are derived from the canonical catalog
# (services/scrummaster/config/agents.json), never hardcoded here. Once the
# field exists, scripts/reconcile-agent-field.sh keeps its options in sync
# with later catalog changes.
CATALOG_PATH="$REPO_ROOT/services/scrummaster/config/agents.json"
AGENT_ID=$(get_existing_field_id "Agent")
if [[ -n "$AGENT_ID" ]]; then
  echo "  Agent field already exists: $AGENT_ID"
else
  echo "  Creating Agent field..."
  AGENT_ID=$(create_field "Agent" "com.atlassian.jira.plugin.system.customfieldtypes:select")
  CONTEXT_ID=$(get_context_id "$AGENT_ID")
  mapfile -t CATALOG_AGENT_IDS < <(jq -r '.agents[].id' "$CATALOG_PATH")
  add_options "$AGENT_ID" "$CONTEXT_ID" "${CATALOG_AGENT_IDS[@]}"
  echo "  Created: $AGENT_ID (options: ${CATALOG_AGENT_IDS[*]})"
fi

echo ""

# Blocked field (single-select: "Yes" = blocked, null = clear)
BLOCKED_ID=$(get_existing_field_id "Blocked")
if [[ -n "$BLOCKED_ID" ]]; then
  echo "  Blocked field already exists: $BLOCKED_ID"
else
  echo "  Creating Blocked field..."
  BLOCKED_ID=$(create_field "Blocked" "com.atlassian.jira.plugin.system.customfieldtypes:select")
  CONTEXT_ID=$(get_context_id "$BLOCKED_ID")
  add_options "$BLOCKED_ID" "$CONTEXT_ID" "Yes"
  echo "  Created: $BLOCKED_ID"
fi

echo ""

# Story schema fields (paragraph type)
declare -A SCHEMA_FIELDS=(
  [VALUE_HYPOTHESIS]="Value Hypothesis"
  [TEST_MEASUREMENT]="Test & Measurement"
  [BEHAVIOR]="Behavior"
  [AC]="Acceptance Criteria"
  [CONSTRAINTS]="Constraints"
  [EDGE_CASES]="Edge Cases"
  [OUT_OF_SCOPE]="Out of Scope"
)

FIELD_TYPE="com.atlassian.jira.plugin.system.customfieldtypes:textarea"

for var_suffix in VALUE_HYPOTHESIS TEST_MEASUREMENT BEHAVIOR AC CONSTRAINTS EDGE_CASES OUT_OF_SCOPE; do
  field_name="${SCHEMA_FIELDS[$var_suffix]}"
  var_ref="${var_suffix}_ID"
  existing=$(get_existing_field_id "$field_name")
  if [[ -n "$existing" ]]; then
    echo "  \"$field_name\" field already exists: $existing"
    eval "$var_ref=\"$existing\""
  else
    echo "  Creating \"$field_name\" field..."
    new_id=$(create_field "$field_name" "$FIELD_TYPE")
    eval "$var_ref=\"$new_id\""
    echo "  Created: $new_id"
  fi
done

echo ""

# Write IDs to the platform .env. It normally exists already — the Jira
# credentials above come out of it — so this only covers the case where
# they were exported into the environment instead.
if [[ ! -f "$FIELD_ID_ENV" ]]; then
  echo "# Created by create-jira-fields.sh — see .env.template for every variable" > "$FIELD_ID_ENV"
  echo "" >> "$FIELD_ID_ENV"
fi

write_env_var "JIRA_AGENT_FIELD_ID" "$AGENT_ID"
write_env_var "JIRA_BLOCKED_FIELD_ID" "$BLOCKED_ID"
write_env_var "JIRA_VALUE_HYPOTHESIS_FIELD_ID" "$VALUE_HYPOTHESIS_ID"
write_env_var "JIRA_TEST_MEASUREMENT_FIELD_ID" "$TEST_MEASUREMENT_ID"
write_env_var "JIRA_BEHAVIOR_FIELD_ID" "$BEHAVIOR_ID"
write_env_var "JIRA_AC_FIELD_ID" "$AC_ID"
write_env_var "JIRA_CONSTRAINTS_FIELD_ID" "$CONSTRAINTS_ID"
write_env_var "JIRA_EDGE_CASES_FIELD_ID" "$EDGE_CASES_ID"
write_env_var "JIRA_OUT_OF_SCOPE_FIELD_ID" "$OUT_OF_SCOPE_ID"

echo "Written to $FIELD_ID_ENV:"
echo "  JIRA_AGENT_FIELD_ID=$AGENT_ID"
echo "  JIRA_BLOCKED_FIELD_ID=$BLOCKED_ID"
echo "  JIRA_VALUE_HYPOTHESIS_FIELD_ID=$VALUE_HYPOTHESIS_ID"
echo "  JIRA_TEST_MEASUREMENT_FIELD_ID=$TEST_MEASUREMENT_ID"
echo "  JIRA_BEHAVIOR_FIELD_ID=$BEHAVIOR_ID"
echo "  JIRA_AC_FIELD_ID=$AC_ID"
echo "  JIRA_CONSTRAINTS_FIELD_ID=$CONSTRAINTS_ID"
echo "  JIRA_EDGE_CASES_FIELD_ID=$EDGE_CASES_ID"
echo "  JIRA_OUT_OF_SCOPE_FIELD_ID=$OUT_OF_SCOPE_ID"
echo ""
echo "Next: re-run platform startup, so scripts/startup/derive-env.sh carries these"
echo "ids into services/core/.env before connect_jira runs (REQ-10)."
