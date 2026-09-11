#!/usr/bin/env bash
# create-jira-fields.sh
#
# Creates the Agent and Blocked custom fields on the Jira instance and
# writes their IDs directly into services/scrummaster/.env.
#
# Idempotent: exits immediately if both IDs are already in services/scrummaster/.env,
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
SM_ENV="$REPO_ROOT/services/scrummaster/.env"

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

if [[ -f "$SM_ENV" ]]; then
  ALL_SET=true
  for var in "${ALL_FIELD_VARS[@]}"; do
    if ! grep -qE "^${var}=customfield_" "$SM_ENV"; then
      ALL_SET=false
      break
    fi
  done
  if [[ "$ALL_SET" == true ]]; then
    echo "All field IDs already set in services/scrummaster/.env — nothing to do."
    for var in "${ALL_FIELD_VARS[@]}"; do
      val=$(grep "^${var}=" "$SM_ENV" | cut -d= -f2)
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

# Write or update a KEY=VALUE line in services/scrummaster/.env
write_env_var() {
  local key="$1" value="$2"
  if [[ -f "$SM_ENV" ]] && grep -q "^${key}=" "$SM_ENV"; then
    sed -i "s|^${key}=.*|${key}=${value}|" "$SM_ENV"
  else
    echo "${key}=${value}" >> "$SM_ENV"
  fi
}

# --- Main ---

echo "Checking Jira custom fields..."
echo ""

# Agent field — options are derived from the canonical catalog
# (services/scrummaster/config/agents.json), never hardcoded here. See
# the agent-assignment design REQ-01/REQ-06. Once the field
# exists, scripts/reconcile-agent-field.sh keeps its options in sync with
# later catalog changes.
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

# Write IDs to services/scrummaster/.env (create file if it doesn't exist)
if [[ ! -f "$SM_ENV" ]]; then
  echo "# Auto-generated by create-jira-fields.sh — fill in remaining values" > "$SM_ENV"
  echo "# See services/scrummaster/.env.example for all required variables" >> "$SM_ENV"
  echo "" >> "$SM_ENV"
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

echo "Written to $SM_ENV:"
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
echo "Next: fill in the remaining variables in services/scrummaster/.env (see .env.example)."
