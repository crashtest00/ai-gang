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
            if record and query.get('full') == ['true']:
                record = self.full_work_item(record)
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

    # The keys services/core/workitems/serializers.py's serialize_comment and
    # serialize_artifact write; the enumeration's own records carry more.
    COMMENT_KEYS = ('id', 'work_item_id', 'author', 'body', 'reference_file', 'reference_function',
                    'source_message_id', 'created_at')
    ARTIFACT_KEYS = ('id', 'work_item_id', 'artifact_type', 'reference', 'created_at')

    def full_work_item(self, record):
        """``GET /work-items/<id>?full=true`` (``serialize_work_item_full``):
        the work item with its comments and delivery artifacts embedded, from
        the same records the comment and delivery enumerations serve."""
        def mine(kind, keys):
            rows = [r for r in self.core.records[kind].values() if r['work_item_id'] == record['id']]
            return [{k: r.get(k) for k in keys} for r in sorted(rows, key=lambda r: (r['created_at'], r['id']))]
        return {**record, 'comments': mine('comments', self.COMMENT_KEYS),
                'artifacts': mine('delivery', self.ARTIFACT_KEYS)}

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


# --- REQ-04: a Core passage's named fields, checked against Core's read --------------

def read_source(core, source):
    """The JSON body of a passage's ``source.read``, over HTTP from the same
    Core stub the indexer read."""
    import urllib.request
    with urllib.request.urlopen(f"{core.url}{source['read']}") as response:
        return json.loads(response.read())


def follow(body, path):
    """The value at ``path`` in ``body``: a key, an index, or ``{key: value}``
    selecting the array element whose ``key`` is ``value``."""
    value = body
    for step in path:
        if isinstance(step, dict):
            (key, wanted), = step.items()
            matches = [e for e in value if isinstance(e, dict) and e.get(key) == wanted]
            assert len(matches) == 1, (path, step)
            value = matches[0]
        else:
            value = value[step]
    return value


# The rendering a Core passage may carry around its fields' values, written out
# here rather than taken from index.render: the check is that nothing outside the
# named fields is record text, so it must not come from the code that wrote it.
#
# Each is matched where the rendering puts it, never as a bare word: a field's
# label only at the start of a line and only with its ": ", a history entry's
# "from"/"to" only with the spaces around them, "(none)" only after them, and
# "message" only after "A2A ". Text outside every span must be a sequence of
# these pieces, so a stray record word ("to", "Status") is caught.
_LABELS = ('Work item', 'Type', 'Status', 'Priority', 'External key', 'Assignee', 'Description', 'Behavior',
           'Acceptance criteria', 'Constraints', 'Edge cases', 'Out of scope', 'Value hypothesis',
           'Test measurement', 'Release notes', 'Candidate commit', 'Build', 'Preview', 'Comment by', 'File',
           'Function')
# (piece, only at the start of a line)
RENDERING_PIECES = (
    [(f'{label}: ', True) for label in _LABELS]
    + [('Delivery artifact (', True), ('A2A message', True), ('A2A ', True), ('Artifact ', True)]
    + [(' from (none)', False), (' to (none)', False), (' from ', False), (' to ', False),
       ('):\n', False), ('): ', False), (' (', False), (':\n', False), (': ', False), ('\n', False), (' ', False)])


def _is_rendering(text, start, end) -> bool:
    """Whether ``text[start:end]``, a run outside every span, is a sequence of
    ``RENDERING_PIECES`` each at a place the rendering puts it. A run at the
    passage's start may begin inside a piece, and one at its end may stop
    inside one, because a unit boundary can fall there."""
    def line_start(at):
        return at == 0 or text[at - 1] == '\n'

    reachable = {start}
    if start == 0:  # the passage begins inside a piece: the rest of that piece
        reachable |= {k for piece, _ in RENDERING_PIECES for k in range(1, min(len(piece), end + 1))
                      if text[:k] == piece[len(piece) - k:]}
    for at in range(start, end + 1):
        if at not in reachable:
            continue
        if at == end:
            return True
        for piece, at_line_start in RENDERING_PIECES:
            if at_line_start and not line_start(at):
                continue
            if text.startswith(piece, at) and at + len(piece) <= end:
                reachable.add(at + len(piece))
            elif end == len(text) and piece.startswith(text[at:end]):
                return True
    return False


def unexplained(text, fields, body) -> list:
    """What in a passage its named fields do not account for: a span whose
    text is not the named value's text at ``valueOffset``, or text outside
    every span that is more than the rendering's labels and punctuation, at
    the places the rendering puts them."""
    problems, covered = [], [False] * len(text)
    for entry in fields:
        value = follow(body, entry['path'])
        value_text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, ensure_ascii=False)
        start, end = entry['span']
        piece = text[start:end]
        if not piece or value_text[entry['valueOffset']:entry['valueOffset'] + len(piece)] != piece:
            problems.append(('span', entry['path'], piece))
        for i in range(start, min(end, len(text))):
            covered[i] = True
    at = 0
    while at < len(text):
        if covered[at]:
            at += 1
            continue
        stop = at
        while stop < len(text) and not covered[stop]:
            stop += 1
        if not _is_rendering(text, at, stop):
            problems.append(('outside', text[at:stop]))
        at = stop
    return problems
