"""Shared fixtures for the corpus index tests (test_index_*.py).

The indexer runs as it does in production, ``manage.py run_indexer``,
against real reads:

- ``FakeCore`` serves Core's four enumeration routes over real HTTP, in
  the shape ``services/core/workitems/views.py`` returns them
  (``_enumeration_response``, ``list_a2a_tasks``): ``?cursor=&limit=``, the
  records under the route's key, ``nextCursor`` and ``hasMore``. Its records
  are built here in the shape ``services/core/workitems/serializers.py``
  writes. Its cursor is a change counter: a record changed after the
  cursor is returned again, as Core's ``change_xid`` cursor does.
- memory-api's archive reads are the real views, served by pytest-django's
  live server, over an archive the real copier fills from a temporary
  harness volume (tests/support.py).
- Core's events go to the real stream names, on Memory's own test Redis
  database (Core's test container ``workitem-test-redis-django``), in the
  envelope ``services/core/workitems/relay.py`` publishes.
- The platform corpus is a git repository under ``tmp_path``.
- Library files' extracted text is the extraction record track 2 writes
  (``extraction.models.ExtractionRecord``), created here as its job does.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import uuid
from datetime import datetime, timedelta, timezone as dt_timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

# pytest-django's live server wraps the app in Django's static-files
# handler, which parses STATIC_URL. Memory serves no static files and sets
# none, so the tests give it a placeholder before the server starts.
from django.conf import settings as _django_settings  # noqa: E402

if getattr(_django_settings, 'STATIC_URL', None) is None:
    _django_settings.STATIC_URL = '/static/'

REDIS_TEST_URL = os.environ.get('MEMORY_INDEX_REDIS_TEST_URL', 'redis://localhost:16399/3')
PROJECT = 'proj'


def _now():
    return datetime.now(dt_timezone.utc).isoformat()


class FakeCore:
    ROUTES = {
        '/work-items/changes': ('work_items', 'workItems'),
        '/work-item-comments/changes': ('comments', 'comments'),
        '/work-item-artifacts/changes': ('delivery', 'deliveryArtifacts'),
        '/a2a/tasks': ('a2a', 'tasks'),
    }

    def __init__(self):
        self.records = {'work_items': {}, 'comments': {}, 'delivery': {}, 'a2a': {}}
        self.seq = {'work_items': {}, 'comments': {}, 'delivery': {}, 'a2a': {}}
        self.counter = 0
        self.available = True
        self.reads = []
        self.lock = threading.Lock()
        core = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _json(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                url = urlparse(self.path)
                query = parse_qs(url.query)
                if not core.available:
                    return self._json(503, {'error': 'unavailable'})
                if url.path not in core.ROUTES:
                    return self._json(404, {'error': 'no route'})
                kind, key = core.ROUTES[url.path]
                with core.lock:
                    core.reads.append((url.path, self.headers.get('X-Actor')))
                    since = int(query.get('cursor', ['0'])[0] or 0)
                    limit = int(query.get('limit', ['50'])[0])
                    changed = sorted((s, rid) for rid, s in core.seq[kind].items() if s > since)
                    page = changed[:limit]
                    body = {key: [core.records[kind][rid] for _, rid in page],
                            'nextCursor': str(page[-1][0] if page else since),
                            'hasMore': len(changed) > limit}
                return self._json(200, body)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.server.server_address[1]}'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def put(self, kind, record):
        with self.lock:
            self.counter += 1
            self.records[kind][record['id']] = record
            self.seq[kind][record['id']] = self.counter
        return record

    def delete(self, kind, record_id):
        with self.lock:
            self.records[kind].pop(record_id, None)
            self.seq[kind].pop(record_id, None)

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.records))

    # --- Records in Core's serialized shapes ---------------------------------

    def work_item(self, display_name, description='', *, status='todo', history=(), spec_artifact=None,
                  work_item_id=None, project=PROJECT):
        wid = work_item_id or str(uuid.uuid4())
        record = {
            'id': wid, 'external_key': None, 'type': 'story', 'display_name': display_name,
            'description': description, 'status': status, 'assignee_agent_id': None, 'priority': 'medium',
            'writes_files': [], 'writes_services': [], 'parent_id': None, 'project': project,
            'created_at': _now(), 'updated_at': _now(), 'storyDetail': None, 'releaseDetail': None,
            'links': [], 'artifacts': [], 'comments': [], 'artifact_links': [],
            'history': [{'id': str(uuid.uuid4()), 'work_item_id': wid, 'field': f, 'old_value': o,
                         'new_value': n, 'actor': 'agent', 'occurred_at': _now()} for f, o, n in history],
            'specification_link': ({'work_item_id': wid, 'artifact_id': spec_artifact, 'requirement_id': 'REQ-01'}
                                   if spec_artifact else None),
        }
        return self.put('work_items', record)

    def comment(self, work_item_id, body, *, author='backend-agent', project=PROJECT):
        return self.put('comments', {
            'id': str(uuid.uuid4()), 'work_item_id': work_item_id, 'author': author, 'body': body,
            'reference_file': None, 'reference_function': None, 'source_message_id': None,
            'created_at': _now(), 'project': project})

    def delivery(self, work_item_id, artifact_type, reference, *, project=PROJECT):
        return self.put('delivery', {
            'id': str(uuid.uuid4()), 'work_item_id': work_item_id, 'artifact_type': artifact_type,
            'reference': reference, 'created_at': _now(), 'project': project, 'specification_link': None})

    def a2a_task(self, texts, *, work_item_id=None, task_id=None, project=PROJECT):
        tid = task_id or f'task-{uuid.uuid4()}'
        messages = [{'messageId': f'msg-{uuid.uuid4()}', 'direction': 'inbound', 'stream': 'aigang:gw',
                     'entryId': f'{i}-0', 'role': 'agent', 'parts': [{'kind': 'text', 'text': text}],
                     'referenceMessageId': None, 'metadata': None, 'timestamp': _now(), 'state': 'working',
                     'recordedAt': _now(), 'message': {}, 'envelope': {}, 'artifacts': [], 'verdict': None,
                     'reason': None, 'finishedState': None, 'outcome': None} for i, text in enumerate(texts)]
        return self.put('a2a', {
            'id': tid, 'recorded': True, 'contextId': 'ctx', 'project': project, 'workItemId': work_item_id,
            'associationError': None, 'createdFromMessageId': messages[0]['messageId'] if messages else None,
            'createdAt': _now(), 'state': None, 'stateHistory': [], 'messages': messages, 'artifacts': [],
            'unarchivedVerdicts': [], 'conflicts': []})


@pytest.fixture
def core(settings):
    fake = FakeCore().start()
    settings.MEMORY_CORE_URL = fake.url
    yield fake
    fake.stop()


# --- Events, as Core's relay publishes them ---------------------------------------

def work_item_event(event_type, work_item_id=None, data=None, project=PROJECT):
    outbox_id = str(uuid.uuid4())
    envelope = {'schemaVersion': '1', 'messageId': outbox_id, 'kind': 'work_item.event', 'project': project,
                'taskId': None, 'contextId': None, 'correlationId': None, 'createdAt': _now(),
                'payload': {'outboxId': outbox_id, 'eventType': event_type,
                            'workItemId': work_item_id, 'data': data or {}}}
    return {'data': json.dumps(envelope)}


def upload_event(artifact_id, uploaded_at, action='created'):
    envelope = {'schemaVersion': '1', 'messageId': f'msg-{uuid.uuid4()}', 'kind': 'artifact.event',
                'project': '_instance', 'taskId': None, 'contextId': None, 'correlationId': None,
                'createdAt': uploaded_at.isoformat(),
                'payload': {'eventType': 'artifact.uploaded', 'artifactId': artifact_id, 'path': f'x/{artifact_id}',
                            'action': action, 'actor': 'admin', 'uploadedAt': uploaded_at.isoformat()}}
    return {'data': json.dumps(envelope)}


def stream(project=PROJECT):
    return f'aigang:workitems:{project}:events'


@pytest.fixture
def redis_client(settings):
    import redis
    client = redis.Redis.from_url(REDIS_TEST_URL, decode_responses=True)
    client.flushdb()
    settings.MEMORY_REDIS_URL = REDIS_TEST_URL
    settings.MEMORY_INDEX_CONSUMER = 'test-indexer'
    yield client
    client.flushdb()


# --- The archive, through memory-api --------------------------------------------

@pytest.fixture
def archive_api(live_server, settings, roots):
    settings.MEMORY_API_URL = live_server.url
    return live_server.url


# --- The platform corpus: a Source checkout ---------------------------------------

def git(root, *args):
    return subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def checkout(tmp_path, settings):
    root = tmp_path / 'source'
    (root / 'docs').mkdir(parents=True)
    (root / 'setup' / 'graphs' / 'engine').mkdir(parents=True)
    (root / 'docs' / 'UserGuide.md').write_text('# User guide\n\nThe aurora valve is reset with init-aurora.sh.\n')
    (root / 'setup' / 'graphs' / 'engine' / 'README.md').write_text('# Engine\n\nGraphs run in the engine.\n')
    (root / 'services').mkdir()
    (root / 'services' / 'NOTES.md').write_text('# Not platform corpus\n\nOutside docs and setup.\n')
    (root / '.gitignore').write_text('node_modules/\n')
    git(root, 'init', '-q', '-b', 'main')
    git(root, '-c', 'user.name=t', '-c', 'user.email=t@t', 'add', '.')
    git(root, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '-m', 'one')
    ignored = root / 'setup' / 'graphs' / 'engine' / 'node_modules' / 'leftpad'
    ignored.mkdir(parents=True)
    (ignored / 'README.md').write_text('# leftpad\n\nAn untracked dependency README.\n')
    settings.MEMORY_PLATFORM_CORPUS_ROOT = str(root)
    return root


def commit_all(root, message='next'):
    git(root, '-c', 'user.name=t', '-c', 'user.email=t@t', 'add', '-A')
    git(root, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '-m', message)
    return git(root, 'rev-parse', 'HEAD')


# --- Library files: the extraction record --------------------------------------------

_BASE = datetime(2026, 10, 17, 9, 0, tzinfo=dt_timezone.utc)


def extraction(artifact_id=None, text='', *, outcome='extracted', updated_at=None, fmt='application/pdf'):
    import hashlib

    from django.utils import timezone

    from extraction.models import ExtractionRecord
    artifact_id = artifact_id or str(uuid.uuid4())
    updated_at = updated_at or _BASE + timedelta(seconds=len(ExtractionRecord.objects.all()))
    defaults = {'source_updated_at': updated_at, 'source_sha256': hashlib.sha256(text.encode()).hexdigest(),
                'outcome': outcome, 'format': fmt if outcome == 'extracted' else None,
                'text': text if outcome == 'extracted' else None,
                'failure_reason': 'unreadable' if outcome == 'failed' else None, 'extracted_at': timezone.now()}
    ExtractionRecord.objects.update_or_create(artifact_id=artifact_id, defaults=defaults)
    return artifact_id, updated_at


# --- Driving the indexer ------------------------------------------------------------

def index_once():
    from django.core.management import call_command
    call_command('run_indexer', once=True)


def units(**filters):
    from index.models import IndexUnit
    return list(IndexUnit.objects.filter(**filters).order_by('source_key', 'position'))


def search(words, **filters):
    """The index searched directly, as REQ-07's Acceptance does: every unit
    whose text matches all the words."""
    from django.contrib.postgres.search import SearchQuery

    from index.models import IndexUnit
    return list(IndexUnit.objects.filter(search=SearchQuery(words, config='english'), **filters))


# --- Session transcripts whose records chain by parentUuid ----------------------------

def asst(message_id, *blocks):
    from tests.support import conv
    record = conv('assistant', content=list(blocks))
    record['message']['id'] = message_id
    return record


def thinking(text):
    return {'type': 'thinking', 'thinking': text}


def text_block(text):
    return {'type': 'text', 'text': text}


def tool_use(tool_use_id, name, tool_input):
    return {'type': 'tool_use', 'id': tool_use_id, 'name': name, 'input': tool_input}


def prompt(text):
    from tests.support import conv
    record = conv('user')
    record['message'] = {'role': 'user', 'content': text}
    return record


def chained(*records, parent=None):
    """Set each conversational record's ``parentUuid`` to the one before it,
    as the harness writes a chain; bookkeeping records pass through."""
    for record in records:
        if 'uuid' in record:
            record['parentUuid'] = parent
            parent = record['uuid']
    return list(records)


def resolve_session_unit(api_url, unit):
    """Follow a session unit's reference to the archive copy, through
    memory-api, and re-render the records on its lines."""
    import urllib.parse
    import urllib.request

    from index import sessions
    ref = unit.reference
    query = urllib.parse.urlencode({'path': ref['file']})
    with urllib.request.urlopen(f"{api_url}/sessions/{ref['sessionId']}/file?{query}") as response:
        data = response.read()
    by_line = dict(sessions.conversational(data))
    turn = sessions.Turn('', [(n, by_line[n]) for n in ref['turnLines']])
    return sessions.render(turn)[0][ref['offset']:ref['end']], data


# --- One distinctive fact in each class --------------------------------------------

@pytest.fixture
def seeded(core, redis_client, archive_api, checkout, roots, copier):
    """One distinctive fact in each of the seven classes, plus the platform
    corpus (the fixture's checkout)."""
    from tests.support import Session as Src, cost_state, enqueue
    harness, _ = roots
    spec_id, _ = extraction(text='# Spec\n\nREQ-01: the quokka gateway retries three times.\n')
    library_id, _ = extraction(text='Invoice ledger: the walrus budget closes on Fridays.\n')
    item = core.work_item('Calibrate the narwhal sensor', 'The narwhal sensor drifts in cold rooms.',
                          history=[('status', 'todo', 'in_progress')], spec_artifact=spec_id)
    core.comment(item['id'], 'The pelican cache was the culprit.')
    core.delivery(item['id'], 'commit', 'abc1234 fix the axolotl timeout')
    core.a2a_task(['Requesting review of the ocelot migration.'], work_item_id=item['id'])
    s = Src(harness)
    s.append([enqueue()] + chained(prompt('Investigate the flamingo outage.'),
                                  asst('m1', text_block('The flamingo outage came from a full disk.'))) + [cost_state()])
    notes = s.notes_dir()
    (notes / 'MEMORY.md').write_bytes(b'# Notes\n\nThe heron deploy needs a manual approval.\n')
    copier.reconcile()
    return {'item': item, 'spec': spec_id, 'library': library_id, 'session': s}


# --- memory-indexer running in the background ------------------------------------------

class Background:
    """``manage.py run_indexer`` in a thread for ``seconds``, as the
    container runs it, with its own database connection."""

    def __init__(self, seconds):
        import threading
        self.seconds = seconds
        self.error = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        from django.core.management import call_command
        from django.db import connection
        try:
            call_command('run_indexer', max_seconds=self.seconds)
        except Exception as err:  # noqa: BLE001 - reported by join()
            self.error = err
        finally:
            connection.close()

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.thread.join(self.seconds + 30)
        assert not self.thread.is_alive(), 'run_indexer did not stop'
        if self.error is not None and exc[0] is None:
            raise self.error


def wait_until(predicate, timeout=30.0, interval=0.05):
    """Seconds until ``predicate()`` held; fails after ``timeout``."""
    import time
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        if predicate():
            return time.monotonic() - start
        time.sleep(interval)
    raise AssertionError(f'not within {timeout}s')
