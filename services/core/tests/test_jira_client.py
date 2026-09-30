"""
Tests for workitems/jira_client.py — Django/`core`'s confined Jira REST
client (REQ-01, strategy/v5.0/v5.1/features/jira-integration-relocation.md).

REQ-01's acceptance is "every operation [ported] has a Django-side
equivalent, exercised by an automated test against a fixture Jira API" —
not "X happens in the real dispatch or production path" (there is no
running consumer in v5.1; v5.2's writer is this client's first caller), so
the enforcement-point rule does not apply here. Every test below drives
`jira_client` through a real HTTP round trip against `_FixtureJiraServer`,
a minimal stand-in Jira instance defined in this file and bound to
`JIRA_URL` for the duration of the test — never by monkeypatching
`jira_client`'s own request function, so the base URL, Basic Auth header
and JSON (de)serialization are all genuinely exercised, not doubled.
"""

from __future__ import annotations

import base64
import json as json_module
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

import pytest
import requests

from workitems import jira_client

RouteHandler = Callable[[dict[str, list[str]], Any], tuple[int, Any]]


class _FixtureJiraHandler(BaseHTTPRequestHandler):
    """Routes registered per-test via `fixture_jira.routes[(method, path)]
    = handler`, where `handler(query, body) -> (status, json_body)`.
    Every received request is recorded in `fixture_jira.received` so tests
    can assert on the auth header, the query string or the request body
    `jira_client` actually sent."""

    routes: dict[tuple[str, str], RouteHandler] = {}
    received: list[dict[str, Any]] = []

    def _handle(self, method: str) -> None:
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        length = int(self.headers.get('Content-Length') or 0)
        raw_body = self.rfile.read(length) if length else b''
        body = json_module.loads(raw_body) if raw_body else None

        type(self).received.append({
            'method': method,
            'path': parsed.path,
            'query': query,
            'body': body,
            'authorization': self.headers.get('Authorization'),
        })

        handler = type(self).routes.get((method, parsed.path))
        if handler is None:
            self.send_response(404)
            self.end_headers()
            return

        status, payload = handler(query, body)
        data = json_module.dumps(payload).encode('utf-8') if payload is not None else b''
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle('GET')

    def do_POST(self) -> None:
        self._handle('POST')

    def do_PUT(self) -> None:
        self._handle('PUT')

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - matches base signature
        pass  # silence BaseHTTPRequestHandler's default stderr access log


def _basic_auth(email: str, token: str) -> str:
    return 'Basic ' + base64.b64encode(f'{email}:{token}'.encode('utf-8')).decode('ascii')


@pytest.fixture
def fixture_jira(monkeypatch):
    """Starts the fixture Jira API on an ephemeral local port, points
    JIRA_URL/JIRA_EMAIL/JIRA_TOKEN at it, and clears jira_client's
    process-lifetime link-type-id cache so tests don't leak into each
    other."""
    _FixtureJiraHandler.routes = {}
    _FixtureJiraHandler.received = []

    server = ThreadingHTTPServer(('127.0.0.1', 0), _FixtureJiraHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    monkeypatch.setenv('JIRA_URL', f'http://127.0.0.1:{server.server_port}')
    monkeypatch.setenv('JIRA_EMAIL', 'bot@example.com')
    monkeypatch.setenv('JIRA_TOKEN', 'token-123')
    monkeypatch.setenv('JIRA_AGENT_FIELD_ID', 'customfield_10050')

    jira_client._link_type_cache.clear()

    try:
        yield _FixtureJiraHandler
    finally:
        server.shutdown()
        thread.join(timeout=2)


# --- isConfigured -----------------------------------------------------------

def test_is_configured_true_when_every_required_value_is_a_real_one(monkeypatch):
    monkeypatch.setenv('JIRA_URL', 'https://real.atlassian.net')
    monkeypatch.setenv('JIRA_EMAIL', 'bot@real.com')
    monkeypatch.setenv('JIRA_TOKEN', 'real-token')
    monkeypatch.setenv('JIRA_AGENT_FIELD_ID', 'customfield_10050')
    assert jira_client.is_configured() is True


def test_is_configured_false_when_a_value_is_the_shipped_placeholder(monkeypatch):
    monkeypatch.setenv('JIRA_URL', 'https://your-org.atlassian.net')  # the shipped placeholder
    monkeypatch.setenv('JIRA_EMAIL', 'bot@real.com')
    monkeypatch.setenv('JIRA_TOKEN', 'real-token')
    monkeypatch.setenv('JIRA_AGENT_FIELD_ID', 'customfield_10050')
    assert jira_client.is_configured() is False


def test_is_configured_false_when_a_required_var_is_unset(monkeypatch):
    monkeypatch.setenv('JIRA_URL', 'https://real.atlassian.net')
    monkeypatch.setenv('JIRA_EMAIL', 'bot@real.com')
    monkeypatch.delenv('JIRA_TOKEN', raising=False)
    monkeypatch.setenv('JIRA_AGENT_FIELD_ID', 'customfield_10050')
    assert jira_client.is_configured() is False


def test_is_configured_reads_the_platform_env_names_not_scrummasters_own(monkeypatch):
    """REQ-01's one departure from a line-for-line port: the credential
    names are JIRA_URL/JIRA_EMAIL/JIRA_TOKEN, not jira.js's own
    JIRA_BASE_URL/JIRA_USER_EMAIL/JIRA_API_TOKEN."""
    monkeypatch.setenv('JIRA_BASE_URL', 'https://real.atlassian.net')
    monkeypatch.setenv('JIRA_USER_EMAIL', 'bot@real.com')
    monkeypatch.setenv('JIRA_API_TOKEN', 'real-token')
    monkeypatch.delenv('JIRA_URL', raising=False)
    monkeypatch.delenv('JIRA_EMAIL', raising=False)
    monkeypatch.delenv('JIRA_TOKEN', raising=False)
    monkeypatch.setenv('JIRA_AGENT_FIELD_ID', 'customfield_10050')
    assert jira_client.is_configured() is False


# --- adfToText ---------------------------------------------------------------

@pytest.mark.parametrize('node,expected', [
    (None, ''),
    ('', ''),
    ('plain string', 'plain string'),
    ({'type': 'text', 'text': 'hello'}, 'hello'),
    ({'type': 'hardBreak'}, '\n'),
    ({'type': 'paragraph', 'content': [{'type': 'text', 'text': 'a'}, {'type': 'text', 'text': 'b'}]}, 'ab\n'),
    ({'type': 'bulletList', 'content': [
        {'type': 'listItem', 'content': [{'type': 'text', 'text': 'x'}]},
    ]}, 'x\n\n'),
    ({'type': 'unknown'}, ''),
])
def test_adf_to_text_matches_jiras_own_shapes(node, expected):
    assert jira_client.adf_to_text(node) == expected


# --- getIssue ----------------------------------------------------------------

def test_get_issue_parses_story_release_and_comment_fields(fixture_jira, monkeypatch):
    monkeypatch.setenv('JIRA_BLOCKED_FIELD_ID', 'customfield_blocked')
    monkeypatch.setenv('JIRA_VALUE_HYPOTHESIS_FIELD_ID', 'customfield_vh')
    monkeypatch.setenv('JIRA_BEHAVIOR_FIELD_ID', 'customfield_behavior')
    monkeypatch.setenv('JIRA_TARGET_PROJECT_FIELD_ID', 'customfield_target')
    monkeypatch.setenv('JIRA_CANDIDATE_SHA_FIELD_ID', 'customfield_sha')

    def handler(query, body):
        assert set(query['fields'][0].split(',')) >= {
            'summary', 'description', 'status', 'customfield_10050',
            'customfield_blocked', 'customfield_vh', 'customfield_behavior',
            'customfield_target', 'customfield_sha',
        }
        return 200, {
            'key': 'GANG-1',
            'fields': {
                'summary': 'Do the thing',
                'description': {'type': 'doc', 'content': [
                    {'type': 'paragraph', 'content': [{'type': 'text', 'text': 'Desc'}]},
                ]},
                'status': {'name': 'In Progress'},
                'resolution': None,
                'issuetype': {'name': 'Story'},
                'project': {'key': 'GANG', 'name': 'AI Gang'},
                'parent': {'key': 'GANG-0'},
                'customfield_10050': {'value': 'backend-agent'},
                'customfield_blocked': None,
                'customfield_vh': {'type': 'doc', 'content': [
                    {'type': 'paragraph', 'content': [{'type': 'text', 'text': 'Because'}]},
                ]},
                'customfield_behavior': {'type': 'doc', 'content': []},
                'customfield_target': {'key': 'HW', 'name': 'hello-world'},
                'customfield_sha': 'abc123',
                'comment': {'comments': [
                    {'author': {'displayName': 'Alice'}, 'body': 'plain body',
                     'created': '2026-09-30T00:00:00.000Z'},
                    {'author': None, 'body': None, 'created': '2026-09-30T01:00:00.000Z'},
                ]},
            },
        }

    fixture_jira.routes[('GET', '/rest/api/3/issue/GANG-1')] = handler

    issue = jira_client.get_issue('GANG-1')

    assert issue['key'] == 'GANG-1'
    assert issue['summary'] == 'Do the thing'
    assert issue['description'] == 'Desc'
    assert issue['status'] == 'In Progress'
    assert issue['resolution'] is None
    assert issue['issuetype'] == 'Story'
    assert issue['project'] == 'GANG'
    assert issue['projectName'] == 'AI Gang'
    assert issue['parent'] == 'GANG-0'
    assert issue['agent'] == 'backend-agent'  # unwrapped from {'value': ...}
    assert issue['blocked'] is None
    assert issue['valueHypothesis'] == 'Because'
    assert issue['behavior'] is None  # empty ADF -> stripped to '' -> None
    assert issue['targetProject'] == 'HW'
    assert issue['targetProjectName'] == 'hello-world'
    assert issue['candidateSha'] == 'abc123'
    assert issue['comments'] == [
        {'author': 'Alice', 'body': 'plain body', 'timestamp': '2026-09-30T00:00:00.000Z'},
        {'author': 'Unknown', 'body': '', 'timestamp': '2026-09-30T01:00:00.000Z'},
    ]

    auth = fixture_jira.received[0]['authorization']
    assert auth == _basic_auth('bot@example.com', 'token-123')


def test_get_issue_falls_back_to_raw_value_when_select_field_has_no_wrapper(fixture_jira, monkeypatch):
    """Mirrors jira.js's `f[field]?.value ?? f[field]`: a raw scalar (no
    `.value` wrapper) passes through unchanged."""
    def handler(query, body):
        return 200, {
            'key': 'GANG-2',
            'fields': {
                'summary': 'S', 'description': None, 'status': None, 'resolution': None,
                'issuetype': None, 'project': None, 'parent': None,
                'customfield_10050': 'raw-agent-value',
                'comment': None,
            },
        }

    fixture_jira.routes[('GET', '/rest/api/3/issue/GANG-2')] = handler

    issue = jira_client.get_issue('GANG-2')
    assert issue['agent'] == 'raw-agent-value'
    assert issue['comments'] == []


# --- searchIssues --------------------------------------------------------------

def test_search_issues_returns_only_keys_from_first_page(fixture_jira):
    def handler(query, body):
        assert query['jql'] == ['project = GANG']
        assert query['maxResults'] == ['100']
        return 200, {'issues': [{'key': 'GANG-1'}, {'key': 'GANG-2'}]}

    fixture_jira.routes[('GET', '/rest/api/3/search/jql')] = handler

    assert jira_client.search_issues('project = GANG') == [{'key': 'GANG-1'}, {'key': 'GANG-2'}]


# --- postComment ---------------------------------------------------------------

def test_post_comment_wraps_text_in_adf(fixture_jira):
    def handler(query, body):
        assert body == {
            'body': {
                'type': 'doc', 'version': 1,
                'content': [{'type': 'paragraph', 'content': [{'type': 'text', 'text': 'hello there'}]}],
            },
        }
        return 200, {}

    fixture_jira.routes[('POST', '/rest/api/3/issue/GANG-1/comment')] = handler

    jira_client.post_comment('GANG-1', 'hello there')


# --- setField / setAgentField / setBlockedField --------------------------------

def test_set_field_puts_the_raw_field_value(fixture_jira):
    def handler(query, body):
        assert body == {'fields': {'customfield_99': 'v'}}
        return 200, None

    fixture_jira.routes[('PUT', '/rest/api/3/issue/GANG-1')] = handler

    jira_client.set_field('GANG-1', 'customfield_99', 'v')


def test_set_agent_field_wraps_value_in_select_shape(fixture_jira):
    def handler(query, body):
        assert body == {'fields': {'customfield_10050': {'value': 'frontend-agent'}}}
        return 200, None

    fixture_jira.routes[('PUT', '/rest/api/3/issue/GANG-1')] = handler

    jira_client.set_agent_field('GANG-1', 'frontend-agent')


def test_set_agent_field_raises_without_the_field_id(fixture_jira, monkeypatch):
    monkeypatch.delenv('JIRA_AGENT_FIELD_ID', raising=False)
    with pytest.raises(ValueError, match='JIRA_AGENT_FIELD_ID'):
        jira_client.set_agent_field('GANG-1', 'x')


def test_set_blocked_field_true_sends_yes(fixture_jira, monkeypatch):
    monkeypatch.setenv('JIRA_BLOCKED_FIELD_ID', 'customfield_blocked')

    def handler(query, body):
        assert body == {'fields': {'customfield_blocked': {'value': 'Yes'}}}
        return 200, None

    fixture_jira.routes[('PUT', '/rest/api/3/issue/GANG-1')] = handler

    jira_client.set_blocked_field('GANG-1', True)


def test_set_blocked_field_false_clears_it(fixture_jira, monkeypatch):
    monkeypatch.setenv('JIRA_BLOCKED_FIELD_ID', 'customfield_blocked')

    def handler(query, body):
        assert body == {'fields': {'customfield_blocked': None}}
        return 200, None

    fixture_jira.routes[('PUT', '/rest/api/3/issue/GANG-1')] = handler

    jira_client.set_blocked_field('GANG-1', False)


def test_set_blocked_field_raises_without_the_field_id(fixture_jira, monkeypatch):
    monkeypatch.delenv('JIRA_BLOCKED_FIELD_ID', raising=False)
    with pytest.raises(ValueError, match='JIRA_BLOCKED_FIELD_ID'):
        jira_client.set_blocked_field('GANG-1', True)


# --- transitionIssue ------------------------------------------------------------

def test_transition_issue_posts_the_matching_transition_id(fixture_jira):
    def get_handler(query, body):
        return 200, {'transitions': [
            {'id': '11', 'to': {'name': 'In Review'}},
            {'id': '21', 'to': {'name': 'Done'}},
        ]}

    posted = {}

    def post_handler(query, body):
        posted.update(body)
        return 200, None

    fixture_jira.routes[('GET', '/rest/api/3/issue/GANG-1/transitions')] = get_handler
    fixture_jira.routes[('POST', '/rest/api/3/issue/GANG-1/transitions')] = post_handler

    jira_client.transition_issue('GANG-1', 'Done')

    assert posted == {'transition': {'id': '21'}}


def test_transition_issue_skips_silently_when_the_target_status_has_no_transition(fixture_jira, caplog):
    def get_handler(query, body):
        return 200, {'transitions': [{'id': '11', 'to': {'name': 'In Review'}}]}

    fixture_jira.routes[('GET', '/rest/api/3/issue/GANG-1/transitions')] = get_handler
    # Deliberately no POST route registered: a POST here would 404 and raise.

    jira_client.transition_issue('GANG-1', 'Nonexistent Status')

    assert any('Nonexistent Status' in record.message for record in caplog.records)


# --- createIssue / createSubtask -----------------------------------------------

def test_create_issue_posts_fields_and_returns_the_new_key(fixture_jira):
    def handler(query, body):
        assert body['fields']['project'] == {'key': 'GANG'}
        assert body['fields']['issuetype'] == {'name': 'Task'}
        assert body['fields']['summary'] == 'A summary'
        return 200, {'key': 'GANG-42'}

    fixture_jira.routes[('POST', '/rest/api/3/issue')] = handler

    assert jira_client.create_issue('GANG', 'Task', 'A summary', 'A description') == 'GANG-42'


def test_create_issue_falls_back_to_summary_when_no_description(fixture_jira):
    def handler(query, body):
        text = body['fields']['description']['content'][0]['content'][0]['text']
        assert text == 'A summary'
        return 200, {'key': 'GANG-43'}

    fixture_jira.routes[('POST', '/rest/api/3/issue')] = handler

    jira_client.create_issue('GANG', 'Task', 'A summary', None)


def test_create_subtask_includes_agent_field_when_value_given(fixture_jira):
    def handler(query, body):
        assert body['fields']['parent'] == {'key': 'GANG-1'}
        assert body['fields']['issuetype'] == {'name': 'Sub-task'}
        assert body['fields']['customfield_10050'] == {'value': 'backend-agent'}
        return 200, {'key': 'GANG-44'}

    fixture_jira.routes[('POST', '/rest/api/3/issue')] = handler

    key = jira_client.create_subtask('GANG-1', 'GANG', 'Sub work', 'Desc', 'backend-agent')
    assert key == 'GANG-44'


def test_create_subtask_omits_agent_field_when_no_value_given(fixture_jira):
    def handler(query, body):
        assert 'customfield_10050' not in body['fields']
        return 200, {'key': 'GANG-45'}

    fixture_jira.routes[('POST', '/rest/api/3/issue')] = handler

    jira_client.create_subtask('GANG-1', 'GANG', 'Sub work', 'Desc')


# --- getAgentFieldOptions --------------------------------------------------------

def test_get_agent_field_options_maps_context_options(fixture_jira):
    def context_handler(query, body):
        return 200, {'values': [{'id': 'ctx-1'}]}

    def option_handler(query, body):
        return 200, {'values': [
            {'optionId': '1', 'value': 'backend-agent', 'disabled': False},
            {'id': '2', 'value': 'frontend-agent', 'disabled': True},
        ]}

    fixture_jira.routes[('GET', '/rest/api/3/field/customfield_10050/context')] = context_handler
    fixture_jira.routes[('GET', '/rest/api/3/field/customfield_10050/context/ctx-1/option')] = option_handler

    options = jira_client.get_agent_field_options()
    assert options == [
        {'id': '1', 'value': 'backend-agent', 'disabled': False},
        {'id': '2', 'value': 'frontend-agent', 'disabled': True},
    ]


def test_get_agent_field_options_returns_empty_when_no_context_exists(fixture_jira):
    def context_handler(query, body):
        return 200, {'values': []}

    fixture_jira.routes[('GET', '/rest/api/3/field/customfield_10050/context')] = context_handler

    assert jira_client.get_agent_field_options() == []


def test_get_agent_field_options_raises_without_the_field_id(fixture_jira, monkeypatch):
    monkeypatch.delenv('JIRA_AGENT_FIELD_ID', raising=False)
    with pytest.raises(ValueError, match='JIRA_AGENT_FIELD_ID'):
        jira_client.get_agent_field_options()


# --- getBlocksLinkTypeId ----------------------------------------------------------

def test_get_blocks_link_type_id_prefers_the_operator_override_with_no_network_call(fixture_jira, monkeypatch):
    monkeypatch.setenv('JIRA_BLOCKS_LINK_TYPE_ID', 'pinned-id')
    # No route registered for /issueLinkType: a network call here would 404 and raise.
    assert jira_client.get_blocks_link_type_id() == 'pinned-id'


def test_get_blocks_link_type_id_discovers_by_name_and_caches(fixture_jira):
    calls = {'count': 0}

    def handler(query, body):
        calls['count'] += 1
        return 200, {'issueLinkTypes': [
            {'id': '10', 'name': 'Relates'},
            {'id': '20', 'name': 'Blocks', 'outward': 'blocks'},
        ]}

    fixture_jira.routes[('GET', '/rest/api/3/issueLinkType')] = handler

    first = jira_client.get_blocks_link_type_id()
    second = jira_client.get_blocks_link_type_id()

    assert first == second == '20'
    assert calls['count'] == 1, 'the second call must be served from the process-lifetime cache'


def test_get_blocks_link_type_id_raises_when_the_instance_has_no_blocks_type(fixture_jira):
    def handler(query, body):
        return 200, {'issueLinkTypes': [{'id': '10', 'name': 'Relates'}]}

    fixture_jira.routes[('GET', '/rest/api/3/issueLinkType')] = handler

    with pytest.raises(ValueError, match='Blocks'):
        jira_client.get_blocks_link_type_id()


# --- getSubtasksByParent -------------------------------------------------------------

def test_get_subtasks_by_parent_maps_summary_status_and_labels(fixture_jira):
    def handler(query, body):
        assert query['jql'] == ['parent = "GANG-1"']
        return 200, {'issues': [
            {'key': 'GANG-2', 'fields': {'summary': 'Sub A', 'status': {'name': 'To Do'}, 'labels': ['x']}},
        ]}

    fixture_jira.routes[('GET', '/rest/api/3/search/jql')] = handler

    assert jira_client.get_subtasks_by_parent('GANG-1') == [
        {'key': 'GANG-2', 'summary': 'Sub A', 'status': 'To Do', 'labels': ['x']},
    ]


# --- getIssueLinks / createIssueLink --------------------------------------------------

def test_get_issue_links_splits_blocks_and_is_blocked_by(fixture_jira, monkeypatch):
    monkeypatch.setenv('JIRA_BLOCKS_LINK_TYPE_ID', 'link-type-1')

    def handler(query, body):
        return 200, {'fields': {'issuelinks': [
            {'type': {'id': 'link-type-1'}, 'outwardIssue': {'key': 'GANG-2'}},
            {'type': {'id': 'link-type-1'}, 'inwardIssue': {'key': 'GANG-3'}},
            {'type': {'id': 'other-type'}, 'outwardIssue': {'key': 'GANG-9'}},
        ]}}

    fixture_jira.routes[('GET', '/rest/api/3/issue/GANG-1')] = handler

    links = jira_client.get_issue_links('GANG-1')
    assert links == {'blocks': ['GANG-2'], 'isBlockedBy': ['GANG-3']}


def test_create_issue_link_posts_blocker_and_dependent(fixture_jira, monkeypatch):
    monkeypatch.setenv('JIRA_BLOCKS_LINK_TYPE_ID', 'link-type-1')

    def handler(query, body):
        assert body == {
            'type': {'id': 'link-type-1'},
            'outwardIssue': {'key': 'GANG-1'},
            'inwardIssue': {'key': 'GANG-2'},
        }
        return 200, None

    fixture_jira.routes[('POST', '/rest/api/3/issueLink')] = handler

    jira_client.create_issue_link('GANG-1', 'GANG-2')


# --- createSubtaskForProposal ----------------------------------------------------------

def test_create_subtask_for_proposal_sets_no_proposal_label(fixture_jira):
    """REQ-01: proposalLabel/proposalIdFromLabel are not ported, and the
    ported createSubtaskForProposal sets no proposal label — unlike
    jira.js's version, whose `fields.labels` carried one."""
    def handler(query, body):
        assert 'labels' not in body['fields']
        assert body['fields']['issuetype'] == {'name': 'Sub-task'}
        return 200, {'key': 'GANG-50'}

    fixture_jira.routes[('POST', '/rest/api/3/issue')] = handler

    key = jira_client.create_subtask_for_proposal(
        'GANG-1', 'GANG', summary='Proposal subtask', description='desc',
        agent_field_value='backend-agent',
    )
    assert key == 'GANG-50'


def test_module_does_not_export_proposal_label_helpers():
    """jira.js's proposalLabel/proposalIdFromLabel retire rather than
    port (REQ-01) — the encoded-label-as-durable-store workaround
    `core` doesn't need."""
    assert not hasattr(jira_client, 'proposal_label')
    assert not hasattr(jira_client, 'proposal_id_from_label')
    assert not hasattr(jira_client, 'proposalLabel')
    assert not hasattr(jira_client, 'proposalIdFromLabel')


# --- transport-level behaviour (no retry/timeout/backoff) ------------------------------

def test_a_non_2xx_response_raises_instead_of_being_retried(fixture_jira):
    def handler(query, body):
        return 404, {'errorMessages': ['Issue does not exist']}

    fixture_jira.routes[('GET', '/rest/api/3/issue/GANG-404')] = handler

    with pytest.raises(requests.exceptions.HTTPError):
        jira_client.get_issue('GANG-404')
