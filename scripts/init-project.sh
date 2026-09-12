#!/usr/bin/env bash
# init-project.sh
#
# Initialises a new AI Gang project in local mode (local mode is the
# unconditional default; Jira mode cannot be chosen at init):
#   1. Prompts for project name
#   2. Creates the project folder under ~/ai-gang/projects/
#   3. Registers the project (local mode, no Jira project/workflow)
#
# Pass --connect-jira to additionally perform one-time Jira-instance
# bootstrapping for this project (connecting Jira is always a separate,
# later, explicitly-requested operation — never offered as part of the
# default interactive flow above):
#   4. Prompts for a Jira project key
#   5. Runs create-jira-fields.sh (idempotent — creates missing fields, skips existing)
#   6. Registers the Jira webhook
#   7. Creates the Jira project (company-managed Kanban)
#   8. Creates the AI Gang Kanban workflow (idempotent) and assigns it to the project
#   9. Adds all JIRA_*_FIELD_ID fields from services/scrummaster/.env to
#      every screen in the new project (Stories, Tasks, Subtasks)
#
# Usage:
#   ./scripts/init-project.sh [--deployment TYPE] [--desktop-framework tauri|electron] [--connect-jira]
#   ./scripts/init-project.sh --config <file.json> [--connect-jira]
#
# --config <file>: read the project name, deployment target, and stack
# profile from a UTF-8 JSON file (schemaVersion 1; a "project" object with
# nonempty string fields "name", "type", "stack" — see
# setup/graphs/engine/lib/config/) instead of the interactive prompts and
# --deployment flag below. The file is validated — and any invalid input
# rejected, before this project's folder or any service is touched — by
# setup/graphs/engine/lib/config/cli.js, the same validation path a
# supported graph-workflow caller uses. A matching --deployment value is
# accepted; a conflicting one is rejected. Retrying with the same file
# resumes idempotently; retrying with a changed name/type/stack against an
# already-initialised project folder is refused before any further change.
#
# --config also replaces the three questions this script would otherwise
# ask, so an unattended run has nothing to type:
#   - The GitHub repository URL comes from the same file's optional
#     "repository" object ("url"), not from the prompt. With no
#     "repository" object and no terminal, the remote is skipped rather
#     than asked for.
#   - The fine-grained PAT comes from GH_TOKEN in the environment file
#     ($HQ_ENV, read as data — never executed) or from the environment
#     itself, not from the prompt. Without --config an exported GH_TOKEN
#     is deliberately ignored and the prompt is unchanged. With no
#     GH_TOKEN and no terminal, the remote is configured and nothing is
#     pushed.
#   - The final "Continue? [y/N]" confirmation is suppressed when there is
#     no terminal, so an EOF on stdin cannot be read as a refusal. Every
#     decision it covers came from the file and was validated first.
# The "next steps" list printed at the end likewise leaves out the steps
# --config's caller performs itself.
#
# A runnable example lives at scripts/init-project.example.json — copy it
# and edit "name" to try --config directly:
#   ./scripts/init-project.sh --config scripts/init-project.example.json
#
# "type" and "stack" are each one of a fixed, discrete set of identifiers —
# not free text — from the shipped target/stack compatibility catalog
# (setup/graphs/engine/lib/config/catalog.js). Today that catalog supports
# exactly one target and one stack for it:
#   "type":  "web"
#   "stack": "node-express" (the only stack profile supported for "web")
# catalog.js is the authoritative list as it grows — an unsupported value
# for either field is rejected with the current supported choices listed.
#
# Prerequisites:
#   - With --connect-jira: ~/ai-gang/.env contains JIRA_URL, JIRA_EMAIL, JIRA_TOKEN, HQ_URL
#   - ~/ai-gang/.env optionally contains JENKINS_GITHUB_USER (the GitHub username
#     behind Jenkins' github-token credential) — required to apply the beta/prod
#     merge-restriction branch protection from setup/JenkinsConfig.md §7. Without
#     it, dev/beta/prod are still created and dev protection is still applied, but
#     beta/prod protection is skipped with a warning (an incomplete restrictions
#     list would otherwise lock out every pusher, including Jenkins itself).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
HQ_ENV="${HQ_ENV:-$HOME/ai-gang/.env}"
SM_ENV="$REPO_ROOT/services/scrummaster/.env"
# Overridable so tests can point this at a temp directory instead of the
# real projects/ tree — see setup/graphs/engine/test/config-init-cli.test.js.
PROJECTS_DIR="${AIGANG_PROJECTS_DIR:-$REPO_ROOT/projects}"
DEPLOYMENT=""
DESKTOP_FRAMEWORK=""
CONNECT_JIRA=false
CONFIG_FILE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --deployment) DEPLOYMENT="${2:?missing value for --deployment}"; shift 2 ;;
    --desktop-framework) DESKTOP_FRAMEWORK="${2:?missing value for --desktop-framework}"; shift 2 ;;
    --connect-jira) CONNECT_JIRA=true; shift ;;
    --config) CONFIG_FILE="${2:?missing value for --config}"; shift 2 ;;
    -h|--help)
      echo "Usage: $0 [--deployment TYPE] [--desktop-framework tauri|electron] [--connect-jira]"
      echo "       $0 --config <file.json> [--connect-jira]"
      exit 0
      ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

CONFIG_PROJECT_NAME=""
CONFIG_PROJECT_TYPE=""
CONFIG_PROJECT_STACK=""
CONFIG_REPOSITORY_URL=""

# Whether this run has a terminal to ask questions of. A --config run with
# no terminal (platform startup, a pipeline) must never block on a prompt,
# and must never read an EOF on stdin as an answer.
if [[ -t 0 ]]; then
  INTERACTIVE=true
else
  INTERACTIVE=false
fi

if [[ -n "$CONFIG_FILE" ]]; then
  # Validate before anything below can create the project folder or mutate
  # any service — this whole block runs ahead of every prompt and every
  # write. The Node validator is the single shared path used by both this
  # CLI entrypoint and the Initialization Agent's graph workflow
  # (setup/graphs/engine/lib/config/validate.js + catalog.js) — one
  # implementation of "what counts as valid," not two that could drift.
  if [[ ! -f "$CONFIG_FILE" ]]; then
    echo "Error: --config file not found: $CONFIG_FILE" >&2
    exit 1
  fi

  # An unmet dependency this path needs (jq, for the config-identity check
  # below) is reported as a clear diagnostic before any mutation, rather
  # than failing confusingly partway through.
  if ! command -v jq &> /dev/null; then
    echo "Error: --config requires 'jq' (used to record and check the project's configuration identity), but it was not found on PATH." >&2
    exit 1
  fi

  CONFIG_CLI="$REPO_ROOT/setup/graphs/engine/lib/config/cli.js"
  CONFIG_STDERR_FILE=$(mktemp)
  if ! CONFIG_OUTPUT=$(node "$CONFIG_CLI" "$CONFIG_FILE" 2>"$CONFIG_STDERR_FILE"); then
    cat "$CONFIG_STDERR_FILE" >&2
    rm -f "$CONFIG_STDERR_FILE"
    exit 1
  fi
  rm -f "$CONFIG_STDERR_FILE"

  while IFS='=' read -r config_key config_val; do
    case "$config_key" in
      PROJECT_NAME) CONFIG_PROJECT_NAME="$config_val" ;;
      PROJECT_TYPE) CONFIG_PROJECT_TYPE="$config_val" ;;
      PROJECT_STACK) CONFIG_PROJECT_STACK="$config_val" ;;
      REPOSITORY_URL) CONFIG_REPOSITORY_URL="$config_val" ;;
    esac
  done <<< "$CONFIG_OUTPUT"

  if [[ -z "$CONFIG_PROJECT_NAME" || -z "$CONFIG_PROJECT_TYPE" || -z "$CONFIG_PROJECT_STACK" ]]; then
    echo "Error: config validation did not produce the expected project decisions." >&2
    exit 1
  fi

  # A legacy --deployment/--desktop-framework value that matches the
  # config is accepted; a conflicting one is rejected before any mutation.
  # There is nothing else to reconcile here for these three decisions —
  # DEPLOYMENT is otherwise only ever set by --deployment above or by the
  # interactive prompt below, never by an environment variable, so there
  # is no separate "environment variable override" path to guard against.
  if [[ -n "$DEPLOYMENT" && "$DEPLOYMENT" != "$CONFIG_PROJECT_TYPE" ]]; then
    echo "Error: --deployment \"$DEPLOYMENT\" conflicts with project.type \"$CONFIG_PROJECT_TYPE\" in $CONFIG_FILE." >&2
    exit 1
  fi
  if [[ -n "$DESKTOP_FRAMEWORK" && "$CONFIG_PROJECT_TYPE" != "desktop" ]]; then
    echo "Error: --desktop-framework \"$DESKTOP_FRAMEWORK\" conflicts with project.type \"$CONFIG_PROJECT_TYPE\" in $CONFIG_FILE (desktop-framework only applies to a desktop target)." >&2
    exit 1
  fi

  # Bind the validated decisions in place of the interactive/--deployment
  # inputs below — no prompt for these three inputs when a valid config is
  # supplied.
  PROJECT_NAME="$CONFIG_PROJECT_NAME"
  DEPLOYMENT="$CONFIG_PROJECT_TYPE"
fi

if [[ -z "$DEPLOYMENT" ]]; then
  read -rp "Deployment target (web/mobile/desktop/extension/mcp/other): " DEPLOYMENT
fi
if [[ "$DEPLOYMENT" == "desktop" && -z "$DESKTOP_FRAMEWORK" ]]; then
  read -rp "Desktop framework (tauri/electron): " DESKTOP_FRAMEWORK
fi
if [[ "$DEPLOYMENT" == "desktop" && "$DESKTOP_FRAMEWORK" != "tauri" && "$DESKTOP_FRAMEWORK" != "electron" ]]; then
  echo "Error: desktop framework must be 'tauri' or 'electron'." >&2
  exit 2
fi

# --- Load credentials ---
# An environment file is data, not a script. Executing one expands a $, a
# backtick or a $(...) in any value — and one of these values is a
# password somebody invented. So each variable this script uses is read
# out of the file literally, and a value already in the environment wins,
# which is how a caller that has read the file itself hands them over.
#
# This is the whole list: adding a use of a new variable from $HQ_ENV
# means adding it here.
read_env_value() {
  local file="$1" name="$2" line value
  [[ -f "$file" ]] || return 1
  line="$(grep -E "^[[:space:]]*${name}=" "$file" | tail -n 1 || true)"
  [[ -n "$line" ]] || return 1
  value="${line#*=}"
  if [[ ${#value} -ge 2 && "${value:0:1}" == '"' && "${value: -1}" == '"' ]]; then
    value="${value:1:${#value}-2}"
  elif [[ ${#value} -ge 2 && "${value:0:1}" == "'" && "${value: -1}" == "'" ]]; then
    value="${value:1:${#value}-2}"
  fi
  printf '%s' "$value"
}

for env_var in ANTHROPIC_API_KEY GH_TOKEN JIRA_URL JIRA_EMAIL JIRA_TOKEN HQ_URL JENKINS_GITHUB_USER; do
  if [[ -z "${!env_var:-}" ]]; then
    printf -v "$env_var" '%s' "$(read_env_value "$HQ_ENV" "$env_var" || true)"
  fi
done
unset env_var

if [[ "$CONNECT_JIRA" == "true" ]]; then
  : "${JIRA_URL:?JIRA_URL is not set. Check $HQ_ENV}"
  : "${JIRA_EMAIL:?JIRA_EMAIL is not set. Check $HQ_ENV}"
  : "${JIRA_TOKEN:?JIRA_TOKEN is not set. Check $HQ_ENV}"
  : "${HQ_URL:?HQ_URL is not set. Check $HQ_ENV (e.g. http://1.2.3.4:9000)}"

  # Ensure all Jira custom fields exist and IDs are written to services/scrummaster/.env.
  # create-jira-fields.sh is idempotent — skips fields that already exist.
  # Running it here also picks up any new fields added in a later version of the script.
  echo "Checking Jira custom fields..."
  HQ_ENV="$HQ_ENV" bash "$SCRIPT_DIR/create-jira-fields.sh"
  echo ""
fi

# Register the Jira webhook if not already present.
# Uses the legacy /rest/webhooks/1.0/webhook endpoint (works with basic auth).
# The WEBHOOK_SECRET is embedded as a URL query param — the only way to pass it
# via this API — and ScrumMaster validates it from req.query.secret.
ensure_webhook() {
  echo "Checking Jira webhook..."

  # Auto-generate WEBHOOK_SECRET in services/scrummaster/.env if missing
  local secret
  secret=$(grep -E '^WEBHOOK_SECRET=.+' "$SM_ENV" | cut -d= -f2 || true)
  if [[ -z "$secret" ]]; then
    secret=$(openssl rand -hex 32)
    if grep -q '^WEBHOOK_SECRET=' "$SM_ENV"; then
      sed -i "s|^WEBHOOK_SECRET=.*|WEBHOOK_SECRET=$secret|" "$SM_ENV"
    else
      echo "WEBHOOK_SECRET=$secret" >> "$SM_ENV"
    fi
    echo "  Generated WEBHOOK_SECRET and saved to services/scrummaster/.env"
  fi

  local webhook_url="${HQ_URL}/webhook/jira?secret=${secret}"

  # Check if a webhook for this URL already exists
  local existing
  existing=$(curl -s -u "$AUTH" -H "Accept: application/json" \
    "$JIRA_URL/rest/webhooks/1.0/webhook" \
    | jq -r --arg url "$webhook_url" '.[] | select(.url == $url) | .self' 2>/dev/null || true)

  if [[ -n "$existing" ]]; then
    echo "  Webhook already registered — skipping."
    return
  fi

  # Register the webhook
  local result
  result=$(curl -s -u "$AUTH" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json" \
    -X POST "$JIRA_URL/rest/webhooks/1.0/webhook" \
    -d "$(jq -n \
      --arg url "$webhook_url" \
      '{
        name: "AI Gang",
        url: $url,
        events: ["jira:issue_created", "jira:issue_updated"],
        filters: {},
        excludeBody: false
      }')")

  if echo "$result" | jq -e '.self' > /dev/null 2>&1; then
    echo "  Webhook registered: $webhook_url"
  else
    local reason
    reason=$(echo "$result" | jq -r '.messages[0].arguments[0] // .errorMessages[0] // "unknown error"')
    echo "  Warning: webhook auto-registration failed ($reason)."
    echo "  Register manually in Jira Settings → System → Webhooks:"
    echo "    URL:    $webhook_url"
    echo "    Events: jira:issue_created, jira:issue_updated"
    echo "  Note: Jira Cloud requires HTTPS. Point a domain at this server and set HQ_URL accordingly."
  fi
}

if [[ "$CONNECT_JIRA" == "true" ]]; then
  API="$JIRA_URL/rest/api/3"
  AUTH="$JIRA_EMAIL:$JIRA_TOKEN"

  ensure_webhook
  echo ""
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

# Derive a Jira project key from a project name.
# "hello-world" -> "HW", "my-app" -> "MA", "shop" -> "SHOP"
derive_key() {
  local name="$1"
  local key
  # Take first letter of each hyphen/underscore-separated word, uppercase.
  # `grep -oE` emits one match per line, so a name with 3+ matching words
  # (e.g. "hello-world-desktop") leaves embedded newlines that `tr -d '_-'`
  # doesn't touch — strip those in a second pass (not the same `tr -d '_-\n'`
  # set: GNU tr expands `\n` there too, which then reads as a `_` to newline
  # range and errors out) or the joined key fails the caller's own
  # `^[A-Z][A-Z0-9]{1,9}$` validation before anything gets created.
  key=$(echo "$name" | tr '[:lower:]' '[:upper:]' | grep -oE '(^|[-_])[A-Z]' | tr -d '_-' | tr -d '\n')
  if [[ ${#key} -lt 2 ]]; then
    # Fall back: first 4 chars of name, uppercase
    key=$(echo "$name" | tr '[:lower:]' '[:upper:]' | tr -d '_-' | cut -c1-4)
  fi
  echo "$key"
}

# Collect all JIRA_*_FIELD_ID values from services/scrummaster/.env
get_field_ids() {
  grep -E '^JIRA_[A-Z_]+_FIELD_ID=customfield_' "$SM_ENV" | cut -d= -f2
}

# Get all screen IDs associated with a Jira project
get_project_screen_ids() {
  local project_id="$1"

  # Step 1: issue type screen scheme for this project
  local scheme_id
  scheme_id=$(jira_get "/issuetypescreenscheme/project?projectId=$project_id" \
    | jq -r '.values[0].issueTypeScreenScheme.id')

  if [[ -z "$scheme_id" || "$scheme_id" == "null" ]]; then
    echo ""
    return
  fi

  # Step 2: screen scheme IDs from the issue type screen scheme
  local screen_scheme_ids
  screen_scheme_ids=$(jira_get "/issuetypescreenscheme/mapping?issueTypeScreenSchemeId=$scheme_id" \
    | jq -r '.values[].screenSchemeId' | sort -u)

  # Step 3: screen IDs from each screen scheme
  local all_screen_ids=""
  while IFS= read -r ss_id; do
    local ids
    ids=$(jira_get "/screenscheme?id=$ss_id" \
      | jq -r '.values[].screens | to_entries[] | .value | tostring' 2>/dev/null || true)
    all_screen_ids="${all_screen_ids} ${ids}"
  done <<< "$screen_scheme_ids"

  echo "$all_screen_ids" | tr ' ' '\n' | sort -u | grep -v '^$'
}

# Add a field to a screen (first tab), silently ignoring "already exists" errors
add_field_to_screen() {
  local screen_id="$1" field_id="$2"

  local tab_id
  tab_id=$(jira_get "/screens/$screen_id/tabs" | jq -r '.[0].id')
  if [[ -z "$tab_id" || "$tab_id" == "null" ]]; then return; fi

  local response
  response=$(jira_post "/screens/$screen_id/tabs/$tab_id/fields" "{\"fieldId\": \"$field_id\"}")

  # Ignore "field already on screen" errors
  if echo "$response" | jq -e '.errors' > /dev/null 2>&1; then
    local msg
    msg=$(echo "$response" | jq -r '.errors | to_entries[0].value // empty')
    if [[ "$msg" != *"already"* ]]; then
      echo "    Warning: $msg"
    fi
  fi
}

# --- Workflow setup ---
WORKFLOW_NAME="AI Gang Kanban"
STATUS_BACKLOG=""
STATUS_SHOVEL_READY=""
STATUS_IN_PROGRESS=""
STATUS_IN_REVIEW=""
STATUS_DONE=""

# Ensure the 5 workflow statuses exist globally; populate STATUS_* vars
ensure_workflow_statuses() {
  local search
  search=$(jira_get "/statuses/search")

  STATUS_BACKLOG=$(     echo "$search" | jq -r '.values[] | select(.name=="Backlog")       | .id')
  STATUS_SHOVEL_READY=$(echo "$search" | jq -r '.values[] | select(.name=="Shovel Ready")  | .id')
  STATUS_IN_PROGRESS=$( echo "$search" | jq -r '.values[] | select(.name=="In Progress")   | .id')
  STATUS_IN_REVIEW=$(   echo "$search" | jq -r '.values[] | select(.name=="In Review")     | .id')
  STATUS_DONE=$(        echo "$search" | jq -r '.values[] | select(.name=="Done")          | .id')

  # Create any missing statuses
  local to_create='[]'
  [[ -z "$STATUS_SHOVEL_READY" ]] && to_create=$(echo "$to_create" | jq '. + [{"name":"Shovel Ready","statusCategory":"IN_PROGRESS"}]')
  [[ -z "$STATUS_IN_REVIEW"    ]] && to_create=$(echo "$to_create" | jq '. + [{"name":"In Review","statusCategory":"IN_PROGRESS"}]')

  if [[ "$to_create" != "[]" ]]; then
    local created
    created=$(jira_post "/statuses" "$to_create")
    [[ -z "$STATUS_SHOVEL_READY" ]] && STATUS_SHOVEL_READY=$(echo "$created" | jq -r '.[] | select(.name=="Shovel Ready") | .id')
    [[ -z "$STATUS_IN_REVIEW"    ]] && STATUS_IN_REVIEW=$(   echo "$created" | jq -r '.[] | select(.name=="In Review")    | .id')
    echo "    Created missing statuses."
  fi
}

# Create the AI Gang Kanban workflow if it doesn't already exist.
# Uses POST /workflow (singular) which is the classic workflow creation endpoint.
# Response contains entityId (not id) on success.
ensure_ai_gang_workflow() {
  local response
  response=$(jira_post "/workflow" "$(jq -n \
    --arg name "$WORKFLOW_NAME" \
    --arg bs "$STATUS_BACKLOG" \
    --arg sr "$STATUS_SHOVEL_READY" \
    --arg ip "$STATUS_IN_PROGRESS" \
    --arg ir "$STATUS_IN_REVIEW" \
    --arg dn "$STATUS_DONE" \
    '{
      name: $name,
      description: "AI Gang: Backlog → Shovel Ready → In Progress → In Review → Done",
      statuses: [{id:$bs},{id:$sr},{id:$ip},{id:$ir},{id:$dn}],
      transitions: [
        {name:"Create",       to:$bs, type:"initial", from:[]},
        {name:"Backlog",      to:$bs, type:"global",  from:[]},
        {name:"Shovel Ready", to:$sr, type:"global",  from:[]},
        {name:"Start",        to:$ip, type:"global",  from:[]},
        {name:"In Review",    to:$ir, type:"global",  from:[]},
        {name:"Done",         to:$dn, type:"global",  from:[]}
      ]
    }')")

  if echo "$response" | jq -e '.errorMessages' > /dev/null 2>&1; then
    local msg
    msg=$(echo "$response" | jq -r '.errorMessages[0] // empty')
    if [[ "$msg" == *"already"* || "$msg" == *"exist"* || "$msg" == *"in use"* ]]; then
      echo "  Workflow '$WORKFLOW_NAME' already exists — skipping creation."
    else
      echo "  Warning: workflow creation failed: $msg"
      echo "  Configure workflow manually: Project Settings → Workflows."
      return 1
    fi
  else
    echo "  Created workflow '$WORKFLOW_NAME'."
  fi
}

# Assign the AI Gang Kanban workflow to the project's workflow scheme.
# Active schemes require a draft → update → publish cycle.
# Any statuses in the old workflow missing from the new one are mapped to Backlog.
assign_workflow_to_project() {
  local scheme_id
  scheme_id=$(jira_get "/workflowscheme/project?projectId=$PROJECT_ID" \
    | jq -r '.values[0].workflowScheme.id // empty')

  if [[ -z "$scheme_id" ]]; then
    echo "  Warning: no workflow scheme found for project."
    return
  fi

  # Step 1: Create a draft of the active scheme
  local draft
  draft=$(jira_post "/workflowscheme/$scheme_id/createdraft" '{}')
  if echo "$draft" | jq -e '.errorMessages | select(. != null and length > 0)' > /dev/null 2>&1; then
    echo "  Warning: could not create workflow scheme draft: $(echo "$draft" | jq -r '.errorMessages[0]')"
    return
  fi

  # Step 2: Update the draft's default workflow
  local updated_draft response
  updated_draft=$(echo "$draft" | jq --arg wf "$WORKFLOW_NAME" '. + {defaultWorkflow: $wf}')
  response=$(curl -s -u "$AUTH" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json" \
    -X PUT "$API/workflowscheme/$scheme_id/draft" \
    -d "$updated_draft")
  if echo "$response" | jq -e '.errorMessages | select(. != null and length > 0)' > /dev/null 2>&1; then
    echo "  Warning: could not update workflow scheme draft: $(echo "$response" | jq -r '.errorMessages[0]')"
    return
  fi

  # Step 3: Publish — first attempt with no status mappings
  local publish_result
  publish_result=$(curl -s -u "$AUTH" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json" \
    -X POST "$API/workflowscheme/$scheme_id/draft/publish" \
    -d '{"statusMappings":[]}')

  # If there are missing status mappings, extract them from errors and retry
  if [[ -n "$publish_result" ]] && \
     echo "$publish_result" | jq -e '.errorMessages | select(. != null and length > 0)' > /dev/null 2>&1; then

    local mappings
    mappings=$(echo "$publish_result" | jq -r '.errorMessages[]' | \
      while IFS= read -r msg; do
        it=$(echo "$msg" | grep -oP 'Issue type with ID \K[0-9]+' || true)
        st=$(echo "$msg" | grep -oP 'statuses with IDs \K[0-9]+' || true)
        [[ -n "$it" && -n "$st" ]] && \
          echo "{\"issueTypeId\":\"$it\",\"statusId\":\"$st\",\"newStatusId\":\"$STATUS_BACKLOG\"}"
      done | jq -s '.')

    publish_result=$(curl -s -u "$AUTH" \
      -H "Content-Type: application/json" \
      -H "Accept: application/json" \
      -X POST "$API/workflowscheme/$scheme_id/draft/publish" \
      -d "{\"statusMappings\":$mappings}")
  fi

  if [[ -n "$publish_result" ]] && \
     echo "$publish_result" | jq -e '.errorMessages | select(. != null and length > 0)' > /dev/null 2>&1; then
    echo "  Warning: could not publish workflow: $(echo "$publish_result" | jq -r '.errorMessages[0]')"
    echo "  Configure workflow manually: Project Settings → Workflows."
  else
    echo "  Workflow scheme published → '$WORKFLOW_NAME'."
  fi
}

setup_project_workflow() {
  echo ""
  echo "Configuring project workflow..."
  ensure_workflow_statuses
  echo "    Backlog($STATUS_BACKLOG) → Shovel Ready($STATUS_SHOVEL_READY) → In Progress($STATUS_IN_PROGRESS) → In Review($STATUS_IN_REVIEW) → Done($STATUS_DONE)"
  ensure_ai_gang_workflow && assign_workflow_to_project
}

# --- Prompt for project name ---
echo ""
echo "AI Gang — New Project Initialisation"
echo "====================================="
echo ""

if [[ -n "$CONFIG_FILE" ]]; then
  # Already validated and bound above — no prompt.
  PROJECT_NAME="$CONFIG_PROJECT_NAME"
else
  read -rp "Project name (lowercase, hyphens OK — e.g. hello-world): " PROJECT_NAME

  # Validate: lowercase letters, numbers, hyphens only
  if ! echo "$PROJECT_NAME" | grep -qE '^[a-z][a-z0-9-]+$'; then
    echo "Error: project name must start with a letter and contain only lowercase letters, numbers, and hyphens."
    exit 1
  fi
fi

PROJECT_KEY=""
if [[ "$CONNECT_JIRA" == "true" ]]; then
  # Suggest a Jira project key
  SUGGESTED_KEY=$(derive_key "$PROJECT_NAME")
  echo ""
  read -rp "Jira project key [$SUGGESTED_KEY]: " PROJECT_KEY
  PROJECT_KEY="${PROJECT_KEY:-$SUGGESTED_KEY}"
  PROJECT_KEY=$(echo "$PROJECT_KEY" | tr '[:lower:]' '[:upper:]')

  if ! echo "$PROJECT_KEY" | grep -qE '^[A-Z][A-Z0-9]{1,9}$'; then
    echo "Error: project key must be 2–10 uppercase letters/numbers."
    exit 1
  fi
fi

# GitHub remote — must be created by the human before running this script.
# A config carrying a "repository" object supplies it instead of the
# prompt; a --config run with no terminal and no configured URL leaves it
# blank (the same as answering the prompt blank) rather than blocking or
# reading an EOF as an answer.
GITHUB_URL=""
if [[ -n "$CONFIG_FILE" && -n "$CONFIG_REPOSITORY_URL" ]]; then
  GITHUB_URL="$CONFIG_REPOSITORY_URL"
  echo ""
  echo "  GitHub repository (from $CONFIG_FILE): $GITHUB_URL"
elif [[ -n "$CONFIG_FILE" && "$INTERACTIVE" != "true" ]]; then
  echo ""
  echo "  GitHub: no \"repository\" object in $CONFIG_FILE and no terminal to ask — skipping the remote."
  echo "  Add it manually later with: git -C <project>/src remote add origin <url>"
else
  echo ""
  echo "  GitHub: create the repository on GitHub first, then paste the HTTPS URL below."
  echo "  Leave blank to skip (you can add the remote manually later)."
  read -rp "GitHub repository HTTPS URL (e.g. https://github.com/org/repo.git): " GITHUB_URL
fi

# Fine-grained PAT for container git operations. Under --config it comes
# from the environment — GH_TOKEN in $HQ_ENV, sourced above — so an
# unattended run has nothing to type. Without --config the prompt below is
# unchanged, and an exported GH_TOKEN is deliberately ignored there.
if [[ -n "$CONFIG_FILE" ]]; then
  GH_TOKEN="${GH_TOKEN:-}"
  if [[ -n "$GH_TOKEN" ]]; then
    echo "  GitHub PAT: read from $HQ_ENV."
  fi
else
  GH_TOKEN=""
fi

if [[ -n "$GITHUB_URL" && -z "$GH_TOKEN" ]]; then
  if [[ "$INTERACTIVE" == "true" ]]; then
    echo ""
    echo "  A fine-grained GitHub PAT is required for agents to push branches and open PRs."
    echo "  Generate one at: https://github.com/settings/tokens?type=beta"
    echo "  Repository access: this repo only"
    echo "  Required permissions: Contents (read/write), Pull requests (read/write), Metadata (read)"
    read -rsp "GitHub fine-grained PAT (GH_TOKEN): " GH_TOKEN
    echo ""
  else
    echo ""
    echo "  GitHub PAT: GH_TOKEN is not set in $HQ_ENV and there is no terminal to ask —"
    echo "  the remote will be configured but nothing will be pushed."
  fi
fi

PROJECT_DIR="$PROJECTS_DIR/$PROJECT_NAME"

echo ""
echo "  Project name : $PROJECT_NAME"
echo "  Mode         : $([[ "$CONNECT_JIRA" == "true" ]] && echo "Jira ($PROJECT_KEY)" || echo "local (default — no Jira project or workflow will be created)")"
echo "  Local path   : $PROJECT_DIR"
[[ -n "$GITHUB_URL" ]] && echo "  GitHub       : $GITHUB_URL"
echo ""
if [[ -n "$CONFIG_FILE" && "$INTERACTIVE" != "true" ]]; then
  # Every decision this confirmation covers came from the configuration
  # and has already been validated, and there is no terminal to answer
  # from. Prompting here would read the EOF on stdin as "N" and abort a
  # run nobody declined.
  echo "Continue? [y/N] y   (no terminal — proceeding from $CONFIG_FILE)"
else
  read -rp "Continue? [y/N] " CONFIRM
  if [[ ! "$CONFIRM" =~ ^[Yy]$ ]]; then
    echo "Aborted."
    exit 0
  fi
fi

echo ""

# --- Check project folder ---
if [[ -d "$PROJECT_DIR" ]]; then
  echo "Warning: $PROJECT_DIR already exists — skipping folder creation."
else
  mkdir -p "$PROJECT_DIR/src"
  echo "Created: $PROJECT_DIR (with src/ subdirectory)"
fi

SRC_DIR="$PROJECT_DIR/src"

# --- Config identity ---
# A normalized record of the configured decisions, written once, before any
# further mutation below. A retry with the same --config resumes
# idempotently (this file matches, and every step below is already
# idempotent — see the existing "already exists — skipping" checks). A
# retry with a changed name/type/stack against this same project folder is
# refused here, before touching anything else, rather than merging
# incompatible state.
if [[ -n "$CONFIG_FILE" ]]; then
  CONFIG_IDENTITY_FILE="$PROJECT_DIR/.aigang-config-identity.json"
  if [[ -f "$CONFIG_IDENTITY_FILE" ]]; then
    EXISTING_CONFIG_NAME=$(jq -r '.name' "$CONFIG_IDENTITY_FILE" 2>/dev/null || echo "")
    EXISTING_CONFIG_TYPE=$(jq -r '.type' "$CONFIG_IDENTITY_FILE" 2>/dev/null || echo "")
    EXISTING_CONFIG_STACK=$(jq -r '.stack' "$CONFIG_IDENTITY_FILE" 2>/dev/null || echo "")
    if [[ "$EXISTING_CONFIG_NAME" != "$PROJECT_NAME" || "$EXISTING_CONFIG_TYPE" != "$DEPLOYMENT" || "$EXISTING_CONFIG_STACK" != "$CONFIG_PROJECT_STACK" ]]; then
      echo "Error: $PROJECT_DIR was already initialised with a different configuration" >&2
      echo "  (existing: name=$EXISTING_CONFIG_NAME type=$EXISTING_CONFIG_TYPE stack=$EXISTING_CONFIG_STACK)." >&2
      echo "  Refusing to resume with different decisions (name=$PROJECT_NAME type=$DEPLOYMENT stack=$CONFIG_PROJECT_STACK)." >&2
      exit 1
    fi
    echo "Existing project configuration matches $CONFIG_FILE — resuming idempotently."
  else
    printf '{"schemaVersion":1,"name":"%s","type":"%s","stack":"%s"}\n' \
      "$PROJECT_NAME" "$DEPLOYMENT" "$CONFIG_PROJECT_STACK" > "$CONFIG_IDENTITY_FILE"
    echo "Recorded project configuration identity: $CONFIG_IDENTITY_FILE"
  fi
fi

# Install deployment-specific repository files before the initial commit.
INIT_REPO_ARGS=(--target "$SRC_DIR" --deployment "$DEPLOYMENT")
if [[ -n "$DESKTOP_FRAMEWORK" ]]; then
  INIT_REPO_ARGS+=(--desktop-framework "$DESKTOP_FRAMEWORK")
fi
bash "$SCRIPT_DIR/init-repo.sh" "${INIT_REPO_ARGS[@]}"

# --- Generate project files ---
if [[ ! -f "$PROJECT_DIR/docker-compose.yml" ]]; then
  cat > "$PROJECT_DIR/docker-compose.yml" <<COMPOSE
services:
  dev:
    build: .
    container_name: ${PROJECT_NAME}-dev
    stdin_open: true
    tty: true
    env_file: .env
    volumes:
      - ./src:/workspace
      - ../../setup:/agent-docs:ro
    command: sh -c "gh auth setup-git && tail -f /dev/null"
    restart: unless-stopped
    networks:
      - ai-gang

networks:
  ai-gang:
    external: true
COMPOSE
  echo "Created: $PROJECT_DIR/docker-compose.yml"
fi

if [[ ! -f "$PROJECT_DIR/.env" ]]; then
  # This file holds the Anthropic key and the GitHub PAT, so it is
  # readable only by its owner — the same rule the platform's own derived
  # environment files follow. Created 0600 rather than chmod-ed
  # afterwards, so it is never briefly world-readable.
  ( umask 077
    cat > "$PROJECT_DIR/.env" <<ENVFILE
PROJECT_NAME=${PROJECT_NAME}
REDIS_HOST=ai-gang-redis
ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY:-}
GITHUB_URL=${GITHUB_URL:-}
GH_TOKEN=${GH_TOKEN:-}
ENVFILE
  )
  chmod 600 "$PROJECT_DIR/.env"
  echo "Created: $PROJECT_DIR/.env"
  if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
    echo "  ** ANTHROPIC_API_KEY not found in $HQ_ENV — fill it in before building."
  fi
  if [[ -z "${GH_TOKEN:-}" ]]; then
    echo "  ** GH_TOKEN is blank — fill it in before the first agent run."
    echo "     Generate a fine-grained PAT at: https://github.com/settings/tokens?type=beta"
    echo "     Repository access: this repo only"
    echo "     Required permissions: Contents (read/write), Pull requests (read/write), Metadata (read)"
  fi
fi

# --- Generate CLAUDE.md stub ---
if [[ ! -f "$SRC_DIR/CLAUDE.md" ]]; then
  cat > "$SRC_DIR/CLAUDE.md" <<CLAUDEMD
# ${PROJECT_NAME} — Project Map

<!-- Fill in each section before the first agent runs.
     Agents use this file to locate files efficiently — keep it concise.
     Update at the directory/pattern level when structure changes; not individual files. -->

## Framework / Runtime
<!-- e.g. Node 22 + Express, plain HTML/CSS, React + Vite, Python 3.11 + FastAPI -->

## Key Directories
<!--
- src/           — application source
-->

## Entry Points
<!-- e.g. src/index.html, src/index.js, src/app.py -->

## Conventions
<!-- Non-obvious patterns agents need to follow:
     e.g. CSS modules, named exports only, all API responses wrapped in { data, error } -->

## Test Framework
<!-- e.g. Jest + React Testing Library, pytest, or "none configured" -->
CLAUDEMD
  echo "Created: $SRC_DIR/CLAUDE.md (fill in before first agent run)"
fi

# --- Initialise git repository ---
# The git repo lives in src/ — agents work there and must not see Dockerfile/.env
echo ""
echo "Setting up version control..."

if [[ -d "$SRC_DIR/.git" ]]; then
  echo "  Git repository already exists — skipping init."
else
  git -C "$SRC_DIR" init -q
  echo "  Initialised git repository in src/."
fi

if [[ ! -f "$SRC_DIR/.gitignore" ]]; then
  cat > "$SRC_DIR/.gitignore" <<'GITIGNORE'
node_modules/
*.log
GITIGNORE
  echo "  Created .gitignore"
fi

if [[ -n "$GITHUB_URL" ]]; then
  # Set HTTPS remote
  git -C "$SRC_DIR" remote add origin "$GITHUB_URL" 2>/dev/null || \
    git -C "$SRC_DIR" remote set-url origin "$GITHUB_URL"
  echo "  Remote 'origin' → $GITHUB_URL"

  git -C "$SRC_DIR" add .
  if ! git -C "$SRC_DIR" diff --cached --quiet; then
    git -C "$SRC_DIR" commit -q -m "chore: initialise project"
    echo "  Initial commit created."
  fi

  if [[ -n "$GH_TOKEN" ]]; then
    echo "  Pushing to GitHub..."
    git -C "$SRC_DIR" \
      -c "credential.helper=!f() { echo username=oauth2; echo password=${GH_TOKEN}; }; f" \
      push --set-upstream origin main 2>/dev/null || \
      git -C "$SRC_DIR" \
        -c "credential.helper=!f() { echo username=oauth2; echo password=${GH_TOKEN}; }; f" \
        push --set-upstream origin master
    echo "  Pushed to GitHub."

    # --- Create dev/beta/prod branches + branch protection ---
    # The promoted CI/CD flow (Jenkins auto-merge to dev, automatic dev->beta
    # promotion, PR-gated beta->prod) has nothing to operate on without these
    # three branches existing on the new repo. Protection settings mirror
    # setup/JenkinsConfig.md §7 exactly — do not change these without updating
    # that doc too.
    echo ""
    echo "  Setting up dev/beta/prod branches..."

    DEFAULT_BRANCH=$(git -C "$SRC_DIR" symbolic-ref --short HEAD)
    OWNER_REPO=$(echo "$GITHUB_URL" | sed -E 's#^https://github\.com/##; s#\.git$##')

    if [[ -z "$OWNER_REPO" || "$OWNER_REPO" == "$GITHUB_URL" ]]; then
      echo "  Warning: could not parse '<owner>/<repo>' out of $GITHUB_URL."
      echo "  Create dev/beta/prod and apply protection manually per setup/JenkinsConfig.md §7."
    elif ! command -v gh &> /dev/null; then
      echo "  Warning: 'gh' CLI not found on this host."
      echo "  Create dev/beta/prod and apply protection manually per setup/JenkinsConfig.md §7."
    else
      for branch in dev beta prod; do
        if git -C "$SRC_DIR" ls-remote --exit-code --heads origin "$branch" > /dev/null 2>&1; then
          echo "  Branch '$branch' already exists on origin — skipping creation."
        else
          git -C "$SRC_DIR" \
            -c "credential.helper=!f() { echo username=oauth2; echo password=${GH_TOKEN}; }; f" \
            push origin "${DEFAULT_BRANCH}:refs/heads/${branch}" 2>/dev/null \
            && echo "  Created branch '$branch'." \
            || echo "  Warning: could not create branch '$branch' — create it manually from $DEFAULT_BRANCH."
        fi
      done

      echo "  Applying branch protection (setup/JenkinsConfig.md §7)..."

      # All three calls below build a full JSON body via jq and send it with
      # `gh api --input -`, rather than gh's -f/-F bracket-flag shorthand (which is
      # what setup/JenkinsConfig.md §7's own gh api example uses). Two reasons:
      #   1. `-f` (raw-field) always sends its value as a JSON string — "-f
      #      enforce_admins=true" puts the STRING "true" on the wire, not the
      #      boolean the API requires, and the same goes for every null/boolean/
      #      number field below. `-F` (typed field) would fix that, but:
      #   2. neither -f nor -F can express an explicit empty array. GitHub requires
      #      restrictions.teams to be present (an empty array when there's no team
      #      restriction) — the doc's own `restrictions[teams][]='[]'` doesn't do
      #      that, it appends the literal string "[]" as a team, which GitHub can't
      #      resolve and the whole protection call fails.
      # A jq-built body sidesteps both problems with real JSON types throughout.

      # dev: Jenkins pipeline check must pass, branch must be up to date, no bypassing.
      # required_pull_request_reviews/restrictions are explicitly null — not extra
      # rules, just satisfying the GitHub API's required top-level protection keys
      # for settings the doc doesn't require on this branch.
      DEV_PROTECTION=$(jq -n '{
        required_status_checks: {strict: true, contexts: ["Jenkins pipeline check"]},
        required_pull_request_reviews: null,
        enforce_admins: true,
        restrictions: null
      }')
      GH_TOKEN="$GH_TOKEN" gh api "repos/$OWNER_REPO/branches/dev/protection" -X PUT \
        --input - <<< "$DEV_PROTECTION" > /dev/null 2>&1 \
        && echo "    dev: protected." \
        || echo "    Warning: could not protect 'dev' — configure manually per setup/JenkinsConfig.md §7."

      if [[ -z "${JENKINS_GITHUB_USER:-}" ]]; then
        echo "    Warning: JENKINS_GITHUB_USER not set in $HQ_ENV — skipping 'beta'/'prod'"
        echo "      merge-restriction protection (would otherwise lock out all pushers)."
        echo "      Set it to the GitHub username behind Jenkins' github-token credential,"
        echo "      then apply the beta/prod rules in setup/JenkinsConfig.md §7 manually."
      else
        # beta: no direct pushes — only Jenkins' github-token identity, fast-forward only.
        BETA_PROTECTION=$(jq -n --arg user "$JENKINS_GITHUB_USER" '{
          required_status_checks: null,
          required_pull_request_reviews: null,
          enforce_admins: false,
          required_linear_history: true,
          restrictions: {users: [$user], teams: [], apps: []}
        }')
        GH_TOKEN="$GH_TOKEN" gh api "repos/$OWNER_REPO/branches/beta/protection" -X PUT \
          --input - <<< "$BETA_PROTECTION" > /dev/null 2>&1 \
          && echo "    beta: protected." \
          || echo "    Warning: could not protect 'beta' — configure manually per setup/JenkinsConfig.md §7."

        # prod: PR-only, Jenkins' github-token identity is the sole merger, no bypassing.
        # required_status_checks stays null — §7 notes prod's check requirement is
        # "inherited" (the dev pipeline already validated this SHA before it reached
        # the frozen release PR) — but the key must still be present, since the
        # GitHub API requires all four top-level protection keys even when null.
        PROD_PROTECTION=$(jq -n --arg user "$JENKINS_GITHUB_USER" '{
          required_status_checks: null,
          required_pull_request_reviews: {required_approving_review_count: 0},
          enforce_admins: true,
          restrictions: {users: [$user], teams: [], apps: []}
        }')
        GH_TOKEN="$GH_TOKEN" gh api "repos/$OWNER_REPO/branches/prod/protection" -X PUT \
          --input - <<< "$PROD_PROTECTION" > /dev/null 2>&1 \
          && echo "    prod: protected." \
          || echo "    Warning: could not protect 'prod' — configure manually per setup/JenkinsConfig.md §7."
      fi
    fi
  else
    echo ""
    echo "  Skipping push — GH_TOKEN not provided."
    echo "  When ready, fill in GH_TOKEN in $PROJECT_DIR/.env and push manually:"
    echo "    git -C $SRC_DIR push -u origin main"
    echo "  Then create dev/beta/prod and apply protection per setup/JenkinsConfig.md §7."
  fi
fi

if [[ "$CONNECT_JIRA" == "true" ]]; then
  # --- Create Jira project ---
  echo ""
  echo "Checking Jira..."

  EXISTING_PROJECT=$(jira_get "/project/$PROJECT_KEY" | jq -r '.key // empty' 2>/dev/null || true)
  if [[ -n "$EXISTING_PROJECT" ]]; then
    echo "  Jira project $PROJECT_KEY already exists — skipping creation."
    PROJECT_ID=$(jira_get "/project/$PROJECT_KEY" | jq -r '.id')
  else
    echo "  Creating Jira project $PROJECT_KEY..."
    ACCOUNT_ID=$(jira_get "/myself" | jq -r '.accountId')

    CREATE_RESPONSE=$(jira_post "/project" "$(jq -n \
      --arg key "$PROJECT_KEY" \
      --arg name "$PROJECT_NAME" \
      --arg lead "$ACCOUNT_ID" \
      '{
        key: $key,
        name: $name,
        projectTypeKey: "software",
        projectTemplateKey: "com.pyxis.greenhopper.jira:gh-kanban-template",
        leadAccountId: $lead
      }')")

    PROJECT_ID=$(echo "$CREATE_RESPONSE" | jq -r '.id // empty')
    if [[ -z "$PROJECT_ID" ]]; then
      echo "Error creating Jira project:"
      echo "$CREATE_RESPONSE" | jq .
      exit 1
    fi
    echo "  Created: $PROJECT_KEY (id: $PROJECT_ID)"
  fi

  # --- Configure workflow ---
  setup_project_workflow

  # --- Add fields to project screens ---
  echo ""
  echo "Adding custom fields to project screens..."

  FIELD_IDS=$(get_field_ids)
  SCREEN_IDS=$(get_project_screen_ids "$PROJECT_ID")

  if [[ -z "$SCREEN_IDS" ]]; then
    echo "  Warning: could not determine project screens — add fields manually via Project Settings → Issue Layout."
  else
    while IFS= read -r screen_id; do
      echo "  Screen $screen_id:"
      while IFS= read -r field_id; do
        add_field_to_screen "$screen_id" "$field_id"
        echo "    + $field_id"
      done <<< "$FIELD_IDS"
    done <<< "$SCREEN_IDS"
  fi
fi

# --- Register in services/scrummaster/config/projects.json ---
# Without an entry here, startWebhookConsumers() never starts a consumer for
# this project — events land durably in Redis Streams but are never consumed,
# forever, with no error anywhere. This step is what closes that gap.
echo ""
echo "Registering $PROJECT_NAME in services/scrummaster/config/projects.json..."
# Overridable so tests can point this at a temp fixture instead of the
# real registry — see setup/graphs/engine/test/config-init-cli.test.js.
PROJECTS_CONFIG="${AIGANG_PROJECTS_CONFIG:-$REPO_ROOT/services/scrummaster/config/projects.json}"
if jq -e --arg name "$PROJECT_NAME" '.projects[] | select(.name == $name)' "$PROJECTS_CONFIG" >/dev/null 2>&1; then
  echo "  Already registered — skipping."
else
  TMP_PROJECTS_CONFIG=$(mktemp)
  jq --arg name "$PROJECT_NAME" \
     --arg key "$PROJECT_KEY" \
     '.projects += [{name: $name, jiraProjectKey: (if $key == "" then null else $key end), agents: ["refinement-agent", "backend-agent", "frontend-agent", "devops-agent"]}]' \
     "$PROJECTS_CONFIG" > "$TMP_PROJECTS_CONFIG"
  mv "$TMP_PROJECTS_CONFIG" "$PROJECTS_CONFIG"
  echo "  Added."
fi

# --- Done ---
echo ""
echo "Done."
echo ""
if [[ "$CONNECT_JIRA" != "true" ]]; then
  echo "  Initialised in local mode (default) — no Jira project or workflow was created."
  echo "  To connect this project to Jira later, re-run with --connect-jira."
  echo ""
fi
echo "Next steps:"
echo "  1. Fill in $SRC_DIR/CLAUDE.md (framework, key directories, entry points, conventions)"
next_step=2
# Adding the Dockerfile by hand is the interactive path's step. A
# --config caller installs it from the stack's template itself, straight
# after this script returns, so telling it to write one here would be
# telling it to do the very thing it must not do.
if [[ -z "$CONFIG_FILE" ]]; then
  echo "  $next_step. Add a Dockerfile to $PROJECT_DIR (see Dockerfile-node.template or Dockerfile-python.template)"
  next_step=$((next_step + 1))
fi
echo "  $next_step. docker compose build && docker compose up -d"
next_step=$((next_step + 1))
echo "  $next_step. docker compose exec dev node /agent-docs/subscriber.js &"
echo ""
if [[ -z "${GITHUB_URL:-}" ]]; then
  echo "  Git: repository initialised locally. When you've created the GitHub repo:"
  echo "    git -C $SRC_DIR remote add origin https://github.com/org/repo.git"
  echo "    git -C $SRC_DIR push -u origin main"
  echo "  Then create dev/beta/prod and apply protection per setup/JenkinsConfig.md §7."
  echo ""
fi
