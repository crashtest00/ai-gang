#!/usr/bin/env bash
# create-release-fields.sh
#
# Creates the Jira "Release" issue type, its dedicated fields, and the
# "Abandoned" resolution used to close a release that will not ship — then
# writes the field IDs to services/scrummaster/.env. Sibling to create-jira-fields.sh,
# same idempotent pattern. See the release-workflow design ("Jira
# setup" ToDo section).
#
# The Release issue type itself is instance-level, same as the Story schema
# fields. It still needs to be added to each target project's issue type
# scheme before a Release ticket can be created there — for a company-managed
# Kanban project (the kind init-project.sh creates), that's one manual step:
#   Project Settings → Issue types → Add issue type → Release
# (Jira Cloud's issue-type-scheme API does not reliably support this
# association for all project/plan combinations, so it isn't scripted here —
# same reasoning as the other manual "[HUMAN]" steps in ClaudeInstructions.md.)
#
# Usage:
#   ./scripts/create-release-fields.sh
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
  JIRA_TARGET_PROJECT_FIELD_ID
  JIRA_RELEASE_NOTES_FIELD_ID
  JIRA_CANDIDATE_SHA_FIELD_ID
  JIRA_BUILD_IDENTIFIER_FIELD_ID
  JIRA_PREVIEW_URL_FIELD_ID
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
    echo "All Release field IDs already set in services/scrummaster/.env — nothing to do."
    for var in "${ALL_FIELD_VARS[@]}"; do
      val=$(grep "^${var}=" "$SM_ENV" | cut -d= -f2)
      echo "  ${var}=${val}"
    done
    exit 0
  fi
fi

# --- Helpers (same shape as create-jira-fields.sh) ---

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

write_env_var() {
  local key="$1" value="$2"
  if [[ -f "$SM_ENV" ]] && grep -q "^${key}=" "$SM_ENV"; then
    sed -i "s|^${key}=.*|${key}=${value}|" "$SM_ENV"
  else
    echo "${key}=${value}" >> "$SM_ENV"
  fi
}

# --- Main ---

echo "Checking the Jira Release issue type..."
echo ""

EXISTING_TYPE=$(jira_get "/issuetype" | jq -r '.[] | select(.name == "Release") | .id' | head -1)
if [[ -n "$EXISTING_TYPE" ]]; then
  echo "  Release issue type already exists: $EXISTING_TYPE"
else
  echo "  Creating Release issue type..."
  RESPONSE=$(jira_post "/issuetype" '{"name": "Release", "description": "Batches everything accepted on beta into one SHA-pinned production candidate.", "type": "standard"}')
  EXISTING_TYPE=$(echo "$RESPONSE" | jq -r '.id // empty')
  if [[ -z "$EXISTING_TYPE" ]]; then
    echo "  Error creating Release issue type:"
    echo "$RESPONSE" | jq .
    exit 1
  fi
  echo "  Created: $EXISTING_TYPE"
  echo ""
  echo "  NOTE: add it to each target project — Project Settings → Issue types →"
  echo "        Add issue type → Release (not scripted; see header comment)."
fi

echo ""

# Abandoned resolution — the terminal state for a Release ticket that won't ship.
echo "Checking the Abandoned resolution..."
EXISTING_RES=$(jira_get "/resolution" | jq -r 'if type == "array" then . else (.values // []) end | .[] | select(.name == "Abandoned") | .id' | head -1)
if [[ -n "$EXISTING_RES" ]]; then
  echo "  Abandoned resolution already exists: $EXISTING_RES"
else
  echo "  Creating Abandoned resolution..."
  RES_RESPONSE=$(jira_post "/resolution" '{"name": "Abandoned", "description": "Release ticket closed without shipping — its preview is torn down."}')
  EXISTING_RES=$(echo "$RES_RESPONSE" | jq -r '.id // empty')
  if [[ -z "$EXISTING_RES" ]]; then
    echo "  Warning: could not create Abandoned resolution:"
    echo "$RES_RESPONSE" | jq .
    echo "  Create manually: Jira Settings → Issues → Resolutions."
  else
    echo "  Created: $EXISTING_RES"
  fi
fi

echo ""

# Release fields
echo "Checking Release ticket fields..."
echo ""

# Target Project — required, human-supplied, project-picker type
TARGET_PROJECT_ID=$(get_existing_field_id "Target Project")
if [[ -n "$TARGET_PROJECT_ID" ]]; then
  echo "  \"Target Project\" field already exists: $TARGET_PROJECT_ID"
else
  echo "  Creating \"Target Project\" field..."
  TARGET_PROJECT_ID=$(create_field "Target Project" "com.atlassian.jira.plugin.system.customfieldtypes:project")
  echo "  Created: $TARGET_PROJECT_ID"
fi

# Release Notes — optional, human-supplied, paragraph type
RELEASE_NOTES_ID=$(get_existing_field_id "Release Notes")
if [[ -n "$RELEASE_NOTES_ID" ]]; then
  echo "  \"Release Notes\" field already exists: $RELEASE_NOTES_ID"
else
  echo "  Creating \"Release Notes\" field..."
  RELEASE_NOTES_ID=$(create_field "Release Notes" "com.atlassian.jira.plugin.system.customfieldtypes:textarea")
  echo "  Created: $RELEASE_NOTES_ID"
fi

# Candidate SHA, Build Identifier, Preview URL — automation-populated, text type
declare -A AUTOMATION_FIELDS=(
  [CANDIDATE_SHA]="Candidate SHA"
  [BUILD_IDENTIFIER]="Build Identifier"
  [PREVIEW_URL]="Preview URL"
)

FIELD_TYPE="com.atlassian.jira.plugin.system.customfieldtypes:textfield"

for var_suffix in CANDIDATE_SHA BUILD_IDENTIFIER PREVIEW_URL; do
  field_name="${AUTOMATION_FIELDS[$var_suffix]}"
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

if [[ ! -f "$SM_ENV" ]]; then
  echo "# Auto-generated by create-jira-fields.sh / create-release-fields.sh" > "$SM_ENV"
  echo "# See services/scrummaster/.env.example for all required variables" >> "$SM_ENV"
  echo "" >> "$SM_ENV"
fi

write_env_var "JIRA_TARGET_PROJECT_FIELD_ID" "$TARGET_PROJECT_ID"
write_env_var "JIRA_RELEASE_NOTES_FIELD_ID" "$RELEASE_NOTES_ID"
write_env_var "JIRA_CANDIDATE_SHA_FIELD_ID" "$CANDIDATE_SHA_ID"
write_env_var "JIRA_BUILD_IDENTIFIER_FIELD_ID" "$BUILD_IDENTIFIER_ID"
write_env_var "JIRA_PREVIEW_URL_FIELD_ID" "$PREVIEW_URL_ID"

echo "Written to $SM_ENV:"
echo "  JIRA_TARGET_PROJECT_FIELD_ID=$TARGET_PROJECT_ID"
echo "  JIRA_RELEASE_NOTES_FIELD_ID=$RELEASE_NOTES_ID"
echo "  JIRA_CANDIDATE_SHA_FIELD_ID=$CANDIDATE_SHA_ID"
echo "  JIRA_BUILD_IDENTIFIER_FIELD_ID=$BUILD_IDENTIFIER_ID"
echo "  JIRA_PREVIEW_URL_FIELD_ID=$PREVIEW_URL_ID"
echo ""
echo "Also add these field IDs to the JCasC environment (jenkins/docker-compose.yml)"
echo "and to ~/ai-gang/.env — the release-candidate Jenkins job writes Candidate SHA,"
echo "Build Identifier, and Preview URL directly onto the Release ticket."
echo ""
echo "Next: add the Release issue type to each target project (see note above),"
echo "reuse the existing In Review / Done statuses, and add fields to the Release"
echo "screen the same way init-project.sh does for Story fields."
