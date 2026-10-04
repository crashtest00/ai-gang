"""
The fixture Jira API every Jira-facing test in this suite drives against.

Extracted from `test_jira_client.py`, which defined it for v5.1's client,
because v5.2's outbound writer (canonical-delivery-state.md REQ-09) makes a
real HTTP call on every Jira-mode push — so the webhook-interpretation and
store tests that put a project into Jira mode need the same stand-in Jira,
not a monkeypatched `jira_client`. Keeping one implementation keeps "against
a fixture Jira API" meaning the same thing in every test that claims it: a
real HTTP round trip through `requests`, so the base URL, the Basic Auth
header and the JSON (de)serialization are genuinely exercised.

Routes are registered per test as
`fixture_jira.routes[(method, path)] = handler`, where
`handler(query, body) -> (status, json_body)`. Every received request is
recorded in `fixture_jira.received`, so a test can assert on what was sent.
An unregistered path 404s, which `jira_client` raises on — deliberate: a
test that means to allow a call says so.

`permissive_jira` (below) is the other shape: a catch-all that accepts
anything and records it, for a test whose subject is not the Jira call but
which now makes one.
"""

from __future__ import annotations

import base64
import json as json_module
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

import pytest

from workitems import jira_client

RouteHandler = Callable[[dict[str, list[str]], Any], tuple[int, Any]]


class FixtureJiraHandler(BaseHTTPRequestHandler):
    routes: dict[tuple[str, str], RouteHandler] = {}
    received: list[dict[str, Any]] = []
    # When set, every request this handler has no explicit route for is
    # answered with `(200, catch_all)` and recorded, rather than 404'd.
    catch_all: Any = None

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
        if handler is None and type(self).catch_all is None:
            self.send_response(404)
            self.end_headers()
            return

        if handler is None:
            status, payload = 200, type(self).catch_all
        else:
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


def basic_auth(email: str, token: str) -> str:
    return 'Basic ' + base64.b64encode(f'{email}:{token}'.encode('utf-8')).decode('ascii')


def posted_comment_texts() -> list[str]:
    """Every comment body the writer posted, as plain text — the shape
    `jira_client.post_comment` builds."""
    texts = []
    for request in FixtureJiraHandler.received:
        if request['method'] != 'POST' or not request['path'].endswith('/comment'):
            continue
        content = (((request['body'] or {}).get('body') or {}).get('content') or [])
        for paragraph in content:
            for part in paragraph.get('content') or []:
                if part.get('type') == 'text':
                    texts.append(part['text'])
    return texts


def requests_to(method: str, path_suffix: str) -> list[dict[str, Any]]:
    return [
        request for request in FixtureJiraHandler.received
        if request['method'] == method and request['path'].endswith(path_suffix)
    ]


def _start(monkeypatch, *, catch_all):
    FixtureJiraHandler.routes = {}
    FixtureJiraHandler.received = []
    FixtureJiraHandler.catch_all = catch_all

    server = ThreadingHTTPServer(('127.0.0.1', 0), FixtureJiraHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    monkeypatch.setenv('JIRA_URL', f'http://127.0.0.1:{server.server_port}')
    monkeypatch.setenv('JIRA_EMAIL', 'bot@example.com')
    monkeypatch.setenv('JIRA_TOKEN', 'token-123')
    monkeypatch.setenv('JIRA_AGENT_FIELD_ID', 'customfield_10050')
    monkeypatch.setenv('JIRA_BLOCKED_FIELD_ID', 'customfield_10051')

    jira_client._link_type_cache.clear()

    try:
        yield FixtureJiraHandler
    finally:
        server.shutdown()
        thread.join(timeout=2)
        FixtureJiraHandler.catch_all = None


@pytest.fixture
def fixture_jira(monkeypatch):
    """The strict form: only the routes a test registers are answered."""
    yield from _start(monkeypatch, catch_all=None)


@pytest.fixture
def permissive_jira(monkeypatch):
    """The permissive form: every call is accepted and recorded. For a test
    whose subject is a canonical behaviour that now makes a Jira call on the
    side — the writer's push — rather than the Jira call itself.

    `{}` as the catch-all body is enough for every call the writer makes:
    `post_comment`, `set_field`, `set_agent_field` and `set_blocked_field`
    read nothing from the response, and `transition_issue`'s GET sees no
    `transitions` list, so it reports the transition as unavailable — which
    a test that cares about registers a route for."""
    yield from _start(monkeypatch, catch_all={})


# ---------------------------------------------------------------------------
# A stateful Jira project over the fixture server
# ---------------------------------------------------------------------------

BLOCKS_LINK_TYPE_ID = '10001'

# The statuses the provisioned workflow offers from anywhere: the workflow
# `scripts/init-project.sh` creates makes every transition global
# (`:436-440`), which is the behaviour several requirements reason about.
GLOBAL_STATUSES = ('Backlog', 'Shovel Ready', 'In Progress', 'In Review', 'Done')


class FakeJiraInstance:
    """A small, STATEFUL Jira over `fixture_jira`'s HTTP server: issues with
    a key, an issue type, a status, custom fields and Blocks links, created
    and moved through the same REST paths `jira_client` calls.

    `fixture_jira` on its own answers whatever a test registers, which is
    right for testing one call. REQ-10's push and re-sync, and REQ-11's
    decomposition and mirror, instead make a SEQUENCE of calls whose later
    steps read what the earlier ones wrote — `get_issue_links` after
    `create_issue_link`, `get_issue` after `transition_issue` — so what they
    need is a Jira that remembers. Every route is registered on the fixture,
    so each call is still a real HTTP round trip and still recorded in
    `fixture_jira.received`.

    `transitions` lists the statuses the workflow offers; the default is the
    provisioned workflow's global set. `offered` can be narrowed per issue to
    exercise a missing transition."""

    def __init__(self, handler, *, project_key='TP', agent_field='customfield_10050',
                  blocked_field='customfield_10051'):
        self.handler = handler
        self.project_key = project_key
        self.agent_field = agent_field
        self.blocked_field = blocked_field
        self.issues: dict[str, dict] = {}
        self.links: list[tuple[str, str]] = []
        self.created: list[dict] = []
        self._next = 1

        handler.routes[('GET', '/rest/api/3/issueLinkType')] = lambda q, b: (
            200, {'issueLinkTypes': [{'id': BLOCKS_LINK_TYPE_ID, 'name': 'Blocks',
                                       'outward': 'blocks', 'inward': 'is blocked by'}]}
        )
        handler.routes[('POST', '/rest/api/3/issue')] = self._create
        handler.routes[('POST', '/rest/api/3/issueLink')] = self._link

    # --- creating -------------------------------------------------------

    def add_issue(self, key, *, issuetype='Task', status='Backlog', parent=None, fields=None,
                   offered=None, issue_id=None):
        """Seed an issue that already exists in Jira, as one a previous
        connect or a person created.

        `issue_id` is Jira's own numeric id, which `/issue/{idOrKey}` takes
        as readily as the key — REQ-11's `issuelink_created` body carries
        only ids, so both have to resolve. It defaults to the key's own
        numeric tail."""
        self.issues[key] = {
            'key': key, 'id': str(issue_id or key.split('-')[-1]), 'issuetype': issuetype,
            'status': status, 'parent': parent,
            'fields': dict(fields or {}), 'offered': list(offered) if offered else list(GLOBAL_STATUSES),
        }
        self._register_issue_routes(key)
        return self.issues[key]

    def id_of(self, key):
        return self.issues[key]['id']

    def _create(self, query, body):
        fields = (body or {}).get('fields') or {}
        # Mint the next key nothing holds yet, so a seeded issue (a parent
        # story, an issue a previous connect created) is never overwritten
        # by a create — which would silently point a decomposition record
        # at its own parent.
        while f'{self.project_key}-{self._next}' in self.issues:
            self._next += 1
        key = f'{self.project_key}-{self._next}'
        self._next += 1
        parent = (fields.get('parent') or {}).get('key')
        issue = self.add_issue(
            key,
            issuetype=(fields.get('issuetype') or {}).get('name') or 'Task',
            parent=parent,
            fields={k: v for k, v in fields.items()
                    if k not in ('project', 'summary', 'description', 'issuetype', 'parent')},
        )
        issue['summary'] = fields.get('summary')
        issue['description'] = fields.get('description')
        self.created.append({'key': key, 'fields': fields})
        return 200, {'key': key, 'id': issue['id']}

    def _link(self, query, body):
        body = body or {}
        blocker = (body.get('outwardIssue') or {}).get('key')
        dependent = (body.get('inwardIssue') or {}).get('key')
        self.links.append((blocker, dependent))
        return 201, None

    # --- per-issue routes ------------------------------------------------

    def _register_issue_routes(self, key):
        # By key AND by id: Jira's `/issue/{idOrKey}` resolves either, and
        # `views.py`'s `issuelink_created` branch and the consumer's own
        # reconciliation both look an end up by its id (REQ-11).
        for identifier in (key, self.issues[key]['id']):
            self.handler.routes[('GET', f'/rest/api/3/issue/{identifier}')] = \
                lambda q, b, key=key: (200, self._issue_body(key, q))
        self.handler.routes[('PUT', f'/rest/api/3/issue/{key}')] = \
            lambda q, b, key=key: self._set_fields(key, b)
        self.handler.routes[('GET', f'/rest/api/3/issue/{key}/transitions')] = \
            lambda q, b, key=key: (200, {'transitions': [
                {'id': str(100 + i), 'to': {'name': name}}
                for i, name in enumerate(self.issues[key]['offered'])
            ]})
        self.handler.routes[('POST', f'/rest/api/3/issue/{key}/transitions')] = \
            lambda q, b, key=key: self._transition(key, b)
        self.handler.routes[('POST', f'/rest/api/3/issue/{key}/comment')] = \
            lambda q, b, key=key: (201, {'id': f'{key}-comment-{len(self.handler.received)}'})

    def _issue_body(self, key, query):
        issue = self.issues[key]
        fields = {
            'summary': issue.get('summary'),
            'description': issue.get('description'),
            'status': {'name': issue['status']},
            'issuetype': {'name': issue['issuetype']},
            'project': {'key': self.project_key, 'name': self.project_key},
            **issue['fields'],
        }
        if issue.get('parent'):
            fields['parent'] = {'key': issue['parent']}
        fields['issuelinks'] = [
            {'type': {'id': BLOCKS_LINK_TYPE_ID, 'name': 'Blocks'}, 'inwardIssue': {'key': blocker}}
            for blocker, dependent in self.links if dependent == key
        ] + [
            {'type': {'id': BLOCKS_LINK_TYPE_ID, 'name': 'Blocks'}, 'outwardIssue': {'key': dependent}}
            for blocker, dependent in self.links if blocker == key
        ]
        return {'key': key, 'id': issue['id'], 'fields': fields}

    def _set_fields(self, key, body):
        self.issues[key]['fields'].update(((body or {}).get('fields') or {}))
        return 204, None

    def _transition(self, key, body):
        transition_id = ((body or {}).get('transition') or {}).get('id')
        issue = self.issues[key]
        for i, name in enumerate(issue['offered']):
            if str(100 + i) == str(transition_id):
                issue['status'] = name
                return 204, None
        return 400, {'errorMessages': [f'transition {transition_id} not available']}

    # --- reading, for assertions ------------------------------------------

    def status_of(self, key):
        return self.issues[key]['status']

    def blocked(self, key):
        return self.issues[key]['fields'].get(self.blocked_field)

    def agent(self, key):
        value = self.issues[key]['fields'].get(self.agent_field)
        return value.get('value') if isinstance(value, dict) else value

    def issue_webhook(self, key, event='jira:issue_created', *, changelog=None):
        """The webhook body Jira would send for this issue, in the shape
        `views.jira_webhook` enqueues and `webhook_consumer` reads."""
        body = {'webhookEvent': event, 'issue': self._issue_body(key, {})}
        if changelog:
            body['changelog'] = {'id': f'{key}-changelog', 'items': changelog}
        return body


@pytest.fixture
def jira_instance(permissive_jira):
    """`FakeJiraInstance` over the permissive fixture: every route it
    registers answers from its own state, and any call it does not model is
    still accepted and recorded rather than 404'd."""
    return FakeJiraInstance(permissive_jira)
