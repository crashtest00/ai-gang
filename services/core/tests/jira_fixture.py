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
