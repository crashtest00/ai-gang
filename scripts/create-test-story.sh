#!/usr/bin/env bash
set -euo pipefail

# Load HQ env without exporting sensitive vars to shell output
JIRA_URL=$(grep ^JIRA_URL ~/ai-gang/.env | cut -d= -f2-)
JIRA_EMAIL=$(grep ^JIRA_EMAIL ~/ai-gang/.env | cut -d= -f2-)
JIRA_TOKEN=$(grep ^JIRA_TOKEN ~/ai-gang/.env | cut -d= -f2-)

SUMMARY="${1:-[TEST] Hello World static page}"

curl -s -u "$JIRA_EMAIL:$JIRA_TOKEN" \
  -H "Content-Type: application/json" \
  -X POST "$JIRA_URL/rest/api/3/issue" \
  -d '{
    "fields": {
      "project": { "key": "HW" },
      "summary": "'"$SUMMARY"'",
      "issuetype": { "name": "Story" },
      "customfield_10109": { "type":"doc","version":1,"content":[{"type":"paragraph","content":[{"type":"text","text":"Render a single HTML page with a large centred Hello World heading. Plain HTML and CSS only."}]}] },
      "customfield_10110": { "type":"doc","version":1,"content":[{"type":"paragraph","content":[{"type":"text","text":"Page loads without errors. Heading is visible and centred on screen."}]}] },
      "customfield_10111": { "type":"doc","version":1,"content":[{"type":"paragraph","content":[{"type":"text","text":"Static files only. No build tools. Single index.html file."}]}] },
      "customfield_10112": { "type":"doc","version":1,"content":[{"type":"paragraph","content":[{"type":"text","text":"Must render correctly on mobile viewport widths."}]}] },
      "customfield_10113": { "type":"doc","version":1,"content":[{"type":"paragraph","content":[{"type":"text","text":"Backend, JavaScript, animations, routing."}]}] }
    }
  }' | jq -r '.key'
