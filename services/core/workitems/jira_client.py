"""
Django/`core`'s confined Jira REST client (REQ-01,
strategy/v5.0/v5.1/features/jira-integration-relocation.md).

A line-for-line port of services/scrummaster/src/jira.js's 17 non-proposal
functions — the whole of what that module did, minus `proposalLabel` and
`proposalIdFromLabel`, which encoded a proposal id into a Jira label for
want of a durable store ScrumMaster never had; `core` does not need that
workaround and does not port it. The ported `createSubtaskForProposal`
therefore takes no `proposal_id` and sets no label.

`jira.js` itself has no retry, timeout, backoff or interceptor logic, and
this port adds none beyond the one value it left to v5.2: `TIMEOUT_SECONDS`,
the per-call timeout `_request` now applies, set alongside the client's first
caller (canonical-delivery-state.md §4). A timeout is a transient failure —
the stream redelivers the message, or the caller receives it as an error.

This module is the only code in the running platform that calls Jira on a
work item's behalf. In v5.1 nothing calls it at runtime — it is exercised
only by its own tests (`test_jira_client.py`); v5.2's outbound writer is its
first caller. `workitems/management/commands/ensure_jira_webhook.py` (Jira
webhook *registration*, BF-02) and Jenkins' own Jira writes are the named
exceptions to "only code", and this module does not replace either.

Authenticates from the platform `.env`'s own names — `JIRA_URL`,
`JIRA_EMAIL`, `JIRA_TOKEN` — the one departure from `jira.js`'s own
credential names (`JIRA_BASE_URL`, `JIRA_USER_EMAIL`, `JIRA_API_TOKEN`),
which were ScrumMaster's and retire with it. Every other environment
variable this module reads (the `JIRA_*_FIELD_ID` values, plus
`JIRA_BLOCKS_LINK_TYPE_ID`) is the exact name `jira.js` already reads, since
those already flow into `core` unrenamed.

Every dict this module returns keeps `jira.js`'s own field names (e.g.
`valueHypothesis`, `isBlockedBy`), not a snake_case translation, so a future
caller that already knows that shape — v5.2's writer chief among them —
finds it unchanged.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)

API_PATH = 'rest/api/3'

# canonical-delivery-state.md §4 — "The writer sets the one value the client
# leaves to this stage: a 30-second per-call timeout on `_request`." Without
# it a hung Jira leaves a consumer's handler running for the process's life,
# holding its stream entry pending and its database connection open.
TIMEOUT_SECONDS = 30

# The placeholder values services/scrummaster/.env.example used to ship
# (jira.js:20-24). Matching them literally is a fact about that shipped
# template, not a guess about what an operator typed — carried over
# unchanged even though the template that shipped them retires with
# ScrumMaster's Jira integration (REQ-06).
ENV_PLACEHOLDERS = {
    'https://your-org.atlassian.net',
    'ai-gang-bot@your-domain.com',
    'customfield_XXXXX',
}

# The environment variables no Jira call can succeed without: the instance
# to talk to, the account to talk as, its token, and the Agent field's own
# id. JIRA_URL/JIRA_EMAIL/JIRA_TOKEN are the platform's own names
# (REQ-01's one departure from jira.js's line-for-line text); the field id
# is read by the same name jira.js used.
REQUIRED_ENV = ['JIRA_URL', 'JIRA_EMAIL', 'JIRA_TOKEN', 'JIRA_AGENT_FIELD_ID']

# getBlocksLinkTypeId's process-lifetime cache (jira.js:278,
# `cachedBlocksLinkTypeId`). A module-level dict rather than a bare global so
# tests can reset it without `global` statements reaching into this module.
_link_type_cache: dict[str, str] = {}


def _base_url() -> str:
    return f"{(os.environ.get('JIRA_URL') or '').rstrip('/')}/{API_PATH}"


def _auth() -> tuple[str, str]:
    return (os.environ.get('JIRA_EMAIL') or '', os.environ.get('JIRA_TOKEN') or '')


def _request(method: str, path: str, *, params: dict[str, Any] | None = None,
              json: dict[str, Any] | None = None) -> Any:
    """The one function that talks to Jira. Every ported function below
    calls through here — mirrors jira.js's single `client` axios instance
    (baseURL + basic auth + JSON content type), with no retry, backoff or
    interceptor, same as the original, and with the per-call timeout
    `TIMEOUT_SECONDS` above (the one thing jira.js had none of that this
    port is asked to add)."""
    response = requests.request(
        method,
        f'{_base_url()}{path}',
        auth=_auth(),
        headers={'Content-Type': 'application/json'},
        params=params,
        json=json,
        timeout=TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.json() if response.content else None


# --- isConfigured (jira.js:35-40) ------------------------------------------

def is_configured() -> bool:
    """Whether this installation has a real Jira to talk to at all, as
    opposed to the shipped template's placeholder values."""
    for name in REQUIRED_ENV:
        value = (os.environ.get(name) or '').strip()
        if not value or value in ENV_PLACEHOLDERS:
            return False
    return True


# --- adfToText (jira.js:43-55) ---------------------------------------------

def adf_to_text(node: Any) -> str:
    """Extracts plain text from Atlassian Document Format. Internal to
    this client (not exported by jira.js either), ported because REQ-01
    names it as one of the 17."""
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


# --- getIssue (jira.js:58-123) ---------------------------------------------

def get_issue(key: str) -> dict[str, Any]:
    """Fetch a full issue with every field ScrumMaster's prompt used to
    need."""
    agent_field = os.environ.get('JIRA_AGENT_FIELD_ID')
    blocked_field = os.environ.get('JIRA_BLOCKED_FIELD_ID')
    vh_field = os.environ.get('JIRA_VALUE_HYPOTHESIS_FIELD_ID')
    tm_field = os.environ.get('JIRA_TEST_MEASUREMENT_FIELD_ID')
    behavior_field = os.environ.get('JIRA_BEHAVIOR_FIELD_ID')
    ac_field = os.environ.get('JIRA_AC_FIELD_ID')
    cons_field = os.environ.get('JIRA_CONSTRAINTS_FIELD_ID')
    edge_field = os.environ.get('JIRA_EDGE_CASES_FIELD_ID')
    oos_field = os.environ.get('JIRA_OUT_OF_SCOPE_FIELD_ID')

    # Release ticket fields.
    target_project_field = os.environ.get('JIRA_TARGET_PROJECT_FIELD_ID')
    release_notes_field = os.environ.get('JIRA_RELEASE_NOTES_FIELD_ID')
    candidate_sha_field = os.environ.get('JIRA_CANDIDATE_SHA_FIELD_ID')
    build_id_field = os.environ.get('JIRA_BUILD_IDENTIFIER_FIELD_ID')
    preview_url_field = os.environ.get('JIRA_PREVIEW_URL_FIELD_ID')

    requested_fields = [
        'summary', 'description', 'status', 'comment', 'resolution',
        'issuetype', 'parent', 'project',
        agent_field, blocked_field,
        vh_field, tm_field, behavior_field, ac_field, cons_field, edge_field, oos_field,
        target_project_field, release_notes_field, candidate_sha_field, build_id_field, preview_url_field,
    ]
    fields = ','.join(f for f in requested_fields if f)

    data = _request('GET', f'/issue/{key}', params={'fields': fields})
    f = data.get('fields') or {}

    def _field_text(field_id: Optional[str]) -> Optional[str]:
        if not field_id:
            return None
        return adf_to_text(f.get(field_id)).strip() or None

    def _value_or_raw(raw: Any) -> Any:
        """Mirrors `x?.value ?? x`: a select-field's `{value: ...}`
        wrapper unwraps to its value; anything else (a plain scalar, or a
        dict with no 'value') passes through unchanged."""
        if isinstance(raw, dict):
            value = raw.get('value')
            if value is not None:
                return value
        return raw

    def _prop_or_raw(raw: Any, prop: str) -> Any:
        """Mirrors `x?.prop ?? x`: a picker field's named property, or
        the raw value itself when that property is absent."""
        if isinstance(raw, dict):
            value = raw.get(prop)
            if value is not None:
                return value
        return raw

    status = f.get('status') or {}
    resolution = f.get('resolution') or {}
    issuetype = f.get('issuetype') or {}
    project = f.get('project') or {}
    parent = f.get('parent') or {}
    target_project_raw = f.get(target_project_field) if target_project_field else None

    return {
        'key': data.get('key'),
        'summary': f.get('summary'),
        'description': adf_to_text(f.get('description')).strip(),
        'status': status.get('name'),
        'resolution': resolution.get('name') or None,
        'issuetype': issuetype.get('name'),
        'project': project.get('key'),
        'projectName': project.get('name'),
        'agent': _value_or_raw(f.get(agent_field)) if agent_field else None,
        'blocked': _value_or_raw(f.get(blocked_field)) if blocked_field else None,
        'parent': parent.get('key') or None,
        'comments': [
            {
                'author': ((c.get('author') or {}).get('displayName')) or 'Unknown',
                'body': adf_to_text(c.get('body')).strip(),
                'timestamp': c.get('created'),
            }
            for c in ((f.get('comment') or {}).get('comments') or [])
        ],
        # Story schema fields (paragraph text, may be None if not set).
        'valueHypothesis': _field_text(vh_field),
        'testMeasurement': _field_text(tm_field),
        'behavior': _field_text(behavior_field),
        'acceptanceCriteria': _field_text(ac_field),
        'constraints': _field_text(cons_field),
        'edgeCases': _field_text(edge_field),
        'outOfScope': _field_text(oos_field),
        # Release ticket fields (may be None on non-Release issue types).
        'targetProject': (_prop_or_raw(target_project_raw, 'key') if target_project_field else None) or None,
        'targetProjectName': (target_project_raw.get('name') if isinstance(target_project_raw, dict) else None),
        'releaseNotes': _field_text(release_notes_field),
        'candidateSha': (f.get(candidate_sha_field) if candidate_sha_field else None) or None,
        'buildIdentifier': (f.get(build_id_field) if build_id_field else None) or None,
        'previewUrl': (f.get(preview_url_field) if preview_url_field else None) or None,
    }


# --- searchIssues (jira.js:125-135) ----------------------------------------

def search_issues(jql: str) -> list[dict[str, str]]:
    """Search for issues matching a JQL query. A single bounded lookup,
    not a crawl: takes only the first page, matching jira.js's own
    behaviour."""
    data = _request('GET', '/search/jql', params={'jql': jql, 'fields': 'key', 'maxResults': 100})
    return [{'key': i['key']} for i in (data.get('issues') or [])]


# --- postComment (jira.js:137-149) -----------------------------------------

def post_comment(key: str, text: str) -> None:
    """Post a plain-text comment to an issue."""
    _request('POST', f'/issue/{key}/comment', json={
        'body': {
            'type': 'doc',
            'version': 1,
            'content': [{
                'type': 'paragraph',
                'content': [{'type': 'text', 'text': text}],
            }],
        },
    })


# --- setField (jira.js:151-154) --------------------------------------------

def set_fields(key: str, fields: dict[str, Any]) -> None:
    """Update several fields on an issue in ONE `PUT /issue/{key}` — one
    edit, so one webhook (release-mode-parity.md REQ-13: the candidate SHA,
    build identifier and preview URL reach Jira together)."""
    _request('PUT', f'/issue/{key}', json={'fields': dict(fields)})


def set_field(key: str, field_id: str, value: Any) -> None:
    """Update a single custom field on an issue — a one-field call of
    `set_fields` (REQ-13). Ported because V5.1 REQ-01 names it as one of
    the 17."""
    set_fields(key, {field_id: value})


# --- setAgentField (jira.js:156-161) ---------------------------------------

def set_agent_field(key: str, value: Any) -> None:
    """Set the Agent custom field."""
    field_id = os.environ.get('JIRA_AGENT_FIELD_ID')
    if not field_id:
        raise ValueError('JIRA_AGENT_FIELD_ID not set')
    set_field(key, field_id, {'value': value})


# --- setBlockedField (jira.js:163-170) --------------------------------------

def set_blocked_field(key: str, blocked: bool) -> None:
    """Set the Blocked custom field — a single-select with one option
    "Yes". Pass True to block, False/None to clear."""
    field_id = os.environ.get('JIRA_BLOCKED_FIELD_ID')
    if not field_id:
        raise ValueError('JIRA_BLOCKED_FIELD_ID not set')
    set_field(key, field_id, {'value': 'Yes'} if blocked else None)


# --- transitionIssue (jira.js:172-181) --------------------------------------

def transition_issue(key: str, status_name: str) -> bool:
    """Transition an issue to a named status. Returns True when the
    transition was made, False when the issue's workflow offers no
    transition to that status from where it is.

    jira.js returned nothing and logged a warning, which made a missing
    transition indistinguishable from a successful one. v5.2's writer has to
    tell them apart — a missing transition is an outcome it records, not a
    success (canonical-delivery-state.md REQ-09, "A missing transition is an
    outcome, not a success") — so the skip is reported by this return value.
    The warning stays: a caller that ignores the result behaves exactly as
    it did."""
    data = _request('GET', f'/issue/{key}/transitions')
    transition = next(
        (t for t in (data.get('transitions') or []) if (t.get('to') or {}).get('name') == status_name),
        None,
    )
    if not transition:
        logger.warning('[jira] Transition to "%s" not found for %s — skipping', status_name, key)
        return False
    _request('POST', f'/issue/{key}/transitions', json={'transition': {'id': transition['id']}})
    return True


# --- createIssue (jira.js:183-205) ------------------------------------------

def adf_document(text: Optional[str]) -> dict[str, Any]:
    """Wrap plain text as an Atlassian Document Format document, one
    paragraph per line — the inverse of `adf_to_text` above, and the shape
    Jira's REST API v3 requires for a multi-line rich-text field
    (canonical-delivery-state.md REQ-10: "story fields are sent as
    Atlassian Document Format paragraphs, the shape `jira_interpret` reads
    back with `adf_to_text`").

    One paragraph per line rather than one text node holding newlines,
    because `adf_to_text` appends a newline per paragraph and reads a text
    node verbatim, so this is the shape that round-trips: what
    `jira_interpret.parse_story_fields` reads back off the issue is what
    was sent, modulo the trailing newline it strips. An empty line becomes
    an empty paragraph, which Jira renders as a blank line."""
    lines = (text or '').split('\n')
    return {
        'type': 'doc',
        'version': 1,
        'content': [
            {'type': 'paragraph', 'content': ([{'type': 'text', 'text': line}] if line else [])}
            for line in lines
        ],
    }


def create_issue(project_key: str, issue_type_name: str, summary: str, description: Optional[str],
                  fields: Optional[dict[str, Any]] = None) -> str:
    """Create a top-level issue (not a subtask) for a canonical work item
    with no Jira counterpart yet.

    `fields` is canonical-delivery-state.md REQ-10's addition: extra issue
    fields set in the SAME request as the create, so `connect_jira`'s push
    sets a pushed item's Agent field and, for a story, each story field
    without a second round trip that could half-succeed ("an addition to
    the client, not a second client"). Keys are Jira field ids or names,
    values are whatever Jira's create API takes for them — a
    single-select's `{'value': ...}`, a rich-text field's
    `adf_document(...)`. They are merged over the four fields built here,
    so a caller can override the description it just passed; nothing in
    this stage does."""
    issue_fields: dict[str, Any] = {
        'project': {'key': project_key},
        'summary': summary,
        'description': {
            'type': 'doc',
            'version': 1,
            'content': [{
                'type': 'paragraph',
                'content': [{'type': 'text', 'text': description or summary}],
            }],
        },
        'issuetype': {'name': issue_type_name},
    }
    issue_fields.update(fields or {})
    data = _request('POST', '/issue', json={'fields': issue_fields})
    return data['key']


# --- createSubtask (jira.js:207-230) ----------------------------------------

def create_subtask(parent_key: str, project_key: str, summary: str, description: Optional[str],
                    agent_field_value: Optional[str] = None) -> str:
    """Create a subtask under a parent issue. Returns the new subtask
    key."""
    fields: dict[str, Any] = {
        'project': {'key': project_key},
        'parent': {'key': parent_key},
        'summary': summary,
        'description': {
            'type': 'doc',
            'version': 1,
            'content': [{
                'type': 'paragraph',
                'content': [{'type': 'text', 'text': description}],
            }],
        },
        'issuetype': {'name': 'Sub-task'},
    }

    agent_field = os.environ.get('JIRA_AGENT_FIELD_ID')
    if agent_field and agent_field_value:
        fields[agent_field] = {'value': agent_field_value}

    data = _request('POST', '/issue', json={'fields': fields})
    return data['key']


# --- getAgentFieldOptions (jira.js:232-251) ---------------------------------

def get_agent_field_options() -> list[dict[str, Any]]:
    """Read-only: fetch the current options on the Agent single-select
    field's first context. Used by the periodic Jira catalog-drift audit —
    never used to determine whether an agent identity is valid for
    runtime assignment, which is decided solely by the agents.json
    catalog."""
    field_id = os.environ.get('JIRA_AGENT_FIELD_ID')
    if not field_id:
        raise ValueError('JIRA_AGENT_FIELD_ID not set')

    contexts = _request('GET', f'/field/{field_id}/context')
    context_values = contexts.get('values') or []
    context_id = context_values[0].get('id') if context_values else None
    if not context_id:
        return []

    data = _request('GET', f'/field/{field_id}/context/{context_id}/option')
    return [
        {
            'id': o.get('optionId', o.get('id')),
            'value': o.get('value'),
            'disabled': bool(o.get('disabled')),
        }
        for o in (data.get('values') or [])
    ]


# --- getBlocksLinkTypeId (jira.js:280-300) ----------------------------------

def get_blocks_link_type_id() -> str:
    """Resolve and cache the Jira issue-link-type id whose outward
    relationship is "blocks" (runtime behaviour must key off the id, not
    a mutable display label). JIRA_BLOCKS_LINK_TYPE_ID lets an operator pin
    the id explicitly; otherwise it is discovered once from Jira's
    built-in "Blocks" link type and cached for the life of the process.

    JIRA_BLOCKS_LINK_TYPE_ID is read from the environment when set and
    otherwise discovered through the Jira link-type listing; discovery is
    the supported path. Nothing plumbs the variable into derive-env.sh or
    .env.template. The first link-writing callers are REQ-11's
    (jira_writer.py's `push_link` and connect_jira.py)."""
    cached = _link_type_cache.get('id')
    if cached:
        return cached

    configured = os.environ.get('JIRA_BLOCKS_LINK_TYPE_ID')
    if configured:
        _link_type_cache['id'] = configured
        return configured

    data = _request('GET', '/issueLinkType')
    match = next(
        (
            t for t in (data.get('issueLinkTypes') or [])
            if (t.get('outward') or '').lower() == 'blocks' or (t.get('name') or '').lower() == 'blocks'
        ),
        None,
    )
    if not match:
        raise ValueError(
            '[jira] No "Blocks" issue link type found on this Jira instance and '
            'JIRA_BLOCKS_LINK_TYPE_ID is not set — cannot create or read dependency links'
        )
    _link_type_cache['id'] = match['id']
    return match['id']


# --- getSubtasksByParent (jira.js:302-324) ----------------------------------

def get_subtasks_by_parent(parent_key: str) -> list[dict[str, Any]]:
    """Fetch every Sub-task under a parent issue, with the label and
    status fields dependency handling needs. Used to recover
    already-materialized proposals on redelivery — no local cache is
    kept between calls."""
    data = _request('GET', '/search/jql', params={
        'jql': f'parent = "{parent_key}"',
        'fields': 'summary,status,labels',
        'maxResults': 200,
    })
    return [
        {
            'key': i['key'],
            'summary': (i.get('fields') or {}).get('summary'),
            'status': ((i.get('fields') or {}).get('status') or {}).get('name'),
            'labels': (i.get('fields') or {}).get('labels') or [],
        }
        for i in (data.get('issues') or [])
    ]


# --- getIssueLinks (jira.js:326-342) ----------------------------------------

def get_issue_links(key: str) -> dict[str, list[str]]:
    """Fetch the configured-link-type relationships for one issue,
    normalized to the dependency direction: `blocks` = issues this one
    blocks (outward side), `isBlockedBy` = issues this one is blocked by
    (inward side). Unrelated link types on the issue are ignored."""
    data = _request('GET', f'/issue/{key}', params={'fields': 'issuelinks'})
    link_type_id = get_blocks_link_type_id()

    blocks: list[str] = []
    is_blocked_by: list[str] = []
    for link in (data.get('fields') or {}).get('issuelinks') or []:
        if (link.get('type') or {}).get('id') != link_type_id:
            continue
        outward = link.get('outwardIssue')
        if outward:
            blocks.append(outward['key'])
        inward = link.get('inwardIssue')
        if inward:
            is_blocked_by.append(inward['key'])
    return {'blocks': blocks, 'isBlockedBy': is_blocked_by}


# --- createIssueLink (jira.js:344-355) --------------------------------------

def create_issue_link(blocker_key: str, dependent_key: str) -> None:
    """Create a `blockerKey blocks dependentKey` / `dependentKey is
    blocked by blockerKey` link using the configured link type. Jira does
    not dedupe identical links on repeated creation — callers must check
    get_issue_links first to stay idempotent across redelivery."""
    link_type_id = get_blocks_link_type_id()
    _request('POST', '/issueLink', json={
        'type': {'id': link_type_id},
        'outwardIssue': {'key': blocker_key},
        'inwardIssue': {'key': dependent_key},
    })


# --- createSubtaskForProposal (jira.js:357-386) -----------------------------

def create_subtask_for_proposal(parent_key: str, project_key: str, *, summary: str,
                                 description: Optional[str] = None,
                                 agent_field_value: Optional[str] = None) -> str:
    """Create a subtask for one Refinement Agent decomposition proposal.

    jira.js's version also labeled the subtask with the proposal id (via
    `proposalLabel`) so a later redelivery could recognise it as already
    materialized. REQ-01 does not port `proposalLabel` /
    `proposalIdFromLabel` — that label-as-durable-store workaround existed
    only because ScrumMaster had no store of its own; `core` does — so
    this port takes no `proposal_id` and sets no label. Recognising an
    already-materialized proposal on redelivery is therefore v5.2's to
    design against `core`'s own proposal-to-key record, not this
    function's job."""
    fields: dict[str, Any] = {
        'project': {'key': project_key},
        'parent': {'key': parent_key},
        'summary': summary,
        'description': {
            'type': 'doc',
            'version': 1,
            'content': [{
                'type': 'paragraph',
                'content': [{'type': 'text', 'text': description or ''}],
            }],
        },
        'issuetype': {'name': 'Sub-task'},
    }

    agent_field = os.environ.get('JIRA_AGENT_FIELD_ID')
    if agent_field and agent_field_value:
        fields[agent_field] = {'value': agent_field_value}

    data = _request('POST', '/issue', json={'fields': fields})
    return data['key']
