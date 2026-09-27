"""
Platform-specific interpretation of a Jira
webhook payload lives entirely here (and in webhook_consumer.py, which
calls these functions), never in ScrumMaster or any other Streams client.

Pure functions only: no Django ORM, no Redis, no I/O — everything here is
unit-testable with a plain dict. Mirrors the field-parsing half of
services/scrummaster/src/jira.js's `getIssue`/`adfToText` (the ADF-to-plain-text
conversion and the custom-field-ID-driven story/release field extraction),
applied to a webhook's `issue.fields` payload instead of a live GET
response — the two are the same JSON shape, since Jira's webhook payload
embeds the issue's current field values at delivery time.

Reuses the SAME env var names services/scrummaster/src/jira.js already reads
(JIRA_AGENT_FIELD_ID, JIRA_BLOCKED_FIELD_ID, JIRA_BEHAVIOR_FIELD_ID, ...)
so one .env can configure both services' custom-field ids identically.
"""

from __future__ import annotations

import os
from typing import Any, Optional


def adf_to_text(node: Any) -> str:
    """Extract plain text from Atlassian Document Format — a direct port
    of jira.js's adfToText."""
    if not node:
        return ''
    if isinstance(node, str):
        return node
    if not isinstance(node, dict):
        return ''
    node_type = node.get('type')
    if node_type == 'text':
        return node.get('text') or ''
    if node_type == 'hardBreak':
        return '\n'
    content = node.get('content')
    if isinstance(content, list):
        sep = '\n' if node_type in ('paragraph', 'heading', 'bulletList', 'orderedList', 'listItem') else ''
        return ''.join(adf_to_text(child) for child in content) + sep
    return ''


def _field_id(env_name: str) -> Optional[str]:
    return os.environ.get(env_name) or None


def _text_field(fields: dict, env_name: str) -> Optional[str]:
    field_id = _field_id(env_name)
    if not field_id:
        return None
    value = adf_to_text(fields.get(field_id)).strip()
    return value or None


# The same five fields handlers.js's REQUIRED_STORY_FIELDS gates,
# with the same human-readable labels (used verbatim in the Jira comment
# ScrumMaster's Jira-effects consumer posts, so the operator-facing text is
# byte-for-byte the same as V1).
REQUIRED_STORY_FIELDS = (
    ('behavior', 'Behavior'),
    ('acceptanceCriteria', 'Acceptance Criteria'),
    ('constraints', 'Constraints'),
    ('edgeCases', 'Edge Cases'),
    ('outOfScope', 'Out of Scope'),
)


def parse_story_fields(fields: dict) -> dict[str, Optional[str]]:
    """The seven-field Story schema contract. Title is handled by
    the caller (issue.fields.summary -> display_name); this returns the
    remaining six that live in work_item_story_detail."""
    fields = fields or {}
    return {
        'valueHypothesis': _text_field(fields, 'JIRA_VALUE_HYPOTHESIS_FIELD_ID'),
        'testMeasurement': _text_field(fields, 'JIRA_TEST_MEASUREMENT_FIELD_ID'),
        'behavior': _text_field(fields, 'JIRA_BEHAVIOR_FIELD_ID'),
        'acceptanceCriteria': _text_field(fields, 'JIRA_AC_FIELD_ID'),
        'constraints': _text_field(fields, 'JIRA_CONSTRAINTS_FIELD_ID'),
        'edgeCases': _text_field(fields, 'JIRA_EDGE_CASES_FIELD_ID'),
        'outOfScope': _text_field(fields, 'JIRA_OUT_OF_SCOPE_FIELD_ID'),
    }


def missing_story_fields(detail: dict) -> list[str]:
    """Returns the human-readable labels of any of the five required
    fields that are empty — same rule and same labels as handlers.js's
    REQUIRED_STORY_FIELDS gate."""
    detail = detail or {}
    return [label for key, label in REQUIRED_STORY_FIELDS if not (detail.get(key) or '').strip()]


def parse_agent_field(fields: dict) -> Optional[str]:
    field_id = _field_id('JIRA_AGENT_FIELD_ID')
    if not field_id:
        return None
    value = (fields or {}).get(field_id)
    if isinstance(value, dict):
        return value.get('value')
    return value


# The four Release fields (besides Target Project) that are plain
# text/textarea custom fields — same env-var-driven lookup as
# parse_story_fields's REQUIRED_STORY_FIELDS, keyed to the canonical
# work_item_release_detail column names (BUGFIXES.md BF-01).
RELEASE_TEXT_FIELDS = (
    ('releaseNotes', 'JIRA_RELEASE_NOTES_FIELD_ID'),
    ('candidateSha', 'JIRA_CANDIDATE_SHA_FIELD_ID'),
    ('buildIdentifier', 'JIRA_BUILD_IDENTIFIER_FIELD_ID'),
    ('previewUrl', 'JIRA_PREVIEW_URL_FIELD_ID'),
)


def parse_release_fields(fields: dict) -> dict[str, Optional[str]]:
    """The five-field Release schema contract
    (`scripts/create-release-fields.sh`): Target Project, Release Notes,
    Candidate SHA, Build Identifier, Preview URL. Target Project is a Jira
    project-picker field: {key, name, ...} — the caller maps it onto the
    work item's own `project` (canonical-release-workflow.md REQ-01;
    `work_item_release_detail` has no column for it). The other four are
    plain text/textarea fields, read via `_text_field` exactly like
    parse_story_fields's text fields — `adf_to_text` already handles a
    plain string (Jira Cloud's textarea/textfield custom field types
    return a plain string, not ADF, unlike the native description/comment
    fields)."""
    fields = fields or {}
    target_project_field = _field_id('JIRA_TARGET_PROJECT_FIELD_ID')
    target_project_value = fields.get(target_project_field) if target_project_field else None
    if isinstance(target_project_value, dict):
        target_project_key = target_project_value.get('key')
        target_project_name = target_project_value.get('name')
    else:
        target_project_key = target_project_value
        target_project_name = None

    result: dict[str, Optional[str]] = {
        'targetProjectKey': target_project_key,
        'targetProjectName': target_project_name,
    }
    for key, env_name in RELEASE_TEXT_FIELDS:
        result[key] = _text_field(fields, env_name)
    return result
