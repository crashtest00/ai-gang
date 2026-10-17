"""Shared fixtures for the retrieval tests (test_retrieval_*.py).

Retrieval is exercised through its real route, ``memory-api``'s
``/retrieve``, over the real index that the real indexer fills
(tests/index_support.py). What retrieval reads from Core at query time
(a work item's status, an A2A Task's verdicts, an artifact's record) is
served by ``StatusCore`` in the shapes ``services/core/workitems/views.py``
(``get_work_item``, ``get_a2a_task``) and ``services/core/artifacts/views.py``
(``get_artifact``, ``?record=true``) return them, from the same records
``FakeCore`` enumerates, so a test changes a record once and both the
indexer and retrieval see it. Core logs each such read in ``access_log``;
``StatusCore.access_log`` is that log.
"""

from __future__ import annotations

import json
import urllib.parse
from urllib.parse import urlparse

import pytest

from tests.index_support import FakeCore, PROJECT, _now  # noqa: F401


class StatusCore:
    """Wraps a started ``FakeCore`` so it also answers the three direct
    reads retrieval makes."""

    def __init__(self, core: FakeCore):
        self.core = core
        self.access_log: list = []
        # artifact id -> the record's updatedAt, as Core's artifact record
        # returns it (set by a test; an artifact not set reads 404).
        self.artifacts: dict = {}
        base = core.server.RequestHandlerClass
        outer = self

        class Handler(base):
            def do_GET(self):
                if not outer.handle(self):
                    super().do_GET()

        core.server.RequestHandlerClass = Handler

    def handle(self, handler) -> bool:
        url = urlparse(handler.path)
        parts = [p for p in url.path.split('/') if p]
        query = urllib.parse.parse_qs(url.query)
        actor = handler.headers.get('X-Actor')

        def send(status, body):
            data = json.dumps(body).encode()
            handler.send_response(status)
            handler.send_header('Content-Type', 'application/json')
            handler.send_header('Content-Length', str(len(data)))
            handler.end_headers()
            handler.wfile.write(data)
            return True

        if not self.core.available:
            return False
        if len(parts) == 2 and parts[0] == 'work-items' and parts[1] != 'changes':
            self.access_log.append(('getWorkItem', actor, parts[1]))
            record = self.core.records['work_items'].get(parts[1])
            return send(200, record) if record else send(404, {'error': 'not found'})
        if len(parts) == 3 and parts[:2] == ['a2a', 'tasks']:
            self.access_log.append(('getA2ATask', actor, parts[2]))
            record = self.core.records['a2a'].get(parts[2])
            return send(200, record) if record else send(404, {'error': 'not found'})
        if len(parts) == 2 and parts[0] == 'artifacts' and query.get('record') == ['true']:
            self.access_log.append(('getArtifact', actor, parts[1]))
            updated = self.artifacts.get(parts[1])
            if updated is None:
                return send(404, {'error': 'not found'})
            return send(200, {'id': parts[1], 'path': f'x/{parts[1]}', 'originalFilename': 'f',
                              'uploadedBy': 'admin', 'createdAt': updated, 'updatedAt': updated})
        return False

    def set_verdict(self, task_id, message_index, verdict, reason=None, outcome=None):
        task = self.core.records['a2a'][task_id]
        message = task['messages'][message_index]
        message.update(verdict=verdict, reason=reason, outcome=outcome)
        task['state'] = message['state'] if verdict == 'applied' else task.get('state')
        self.core.put('a2a', task)


@pytest.fixture
def status_core(core):
    return StatusCore(core)


@pytest.fixture
def closed_core(settings):
    """Core is not reachable (a port nothing listens on), so status reads
    report 'unavailable' at once."""
    settings.MEMORY_CORE_URL = 'http://127.0.0.1:9'
    settings.MEMORY_RETRIEVAL_STATUS_TIMEOUT_SECONDS = 2


def ask(client, question, **fields):
    response = client.post('/retrieve', data=json.dumps({'question': question, **fields}),
                           content_type='application/json')
    return response.status_code, response.json()


def passages(client, question, **fields):
    status, body = ask(client, question, **fields)
    assert status == 200, body
    return body['passages']
