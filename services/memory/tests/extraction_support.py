"""Shared fixtures for the artifact text extraction tests (test_extraction_*.py).

``FakeCore`` serves Core's artifact read surface over real HTTP, exactly the
routes ``extraction.core_client`` reads (``services/core/artifacts/views.py``
and ``urls.py``): the bytes as ``application/octet-stream``, the record with
``?record=true``, 404 ``not found`` for an unknown id and 404 ``artifact
file missing`` for a record whose file is gone, plus the listing REQ-04 adds
(track 1). Its state is what the tests change: an upload, a re-upload (new
bytes, new ``updatedAt``) and an in-place edit (new bytes, same
``updatedAt``), as Core's admin and the volume behave.

The extractor runs as the real process (``python -m extractor``) on a spool
under ``tmp_path``. Upload events go to the test Redis (Core's test
container, ``workitem-test-redis-django``), in a database index of
Memory's own, on the real stream name.
"""

from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from datetime import datetime, timedelta, timezone as dt_timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

MEMORY_DIR = Path(__file__).resolve().parent.parent
REDIS_TEST_URL = os.environ.get('MEMORY_REDIS_TEST_URL', 'redis://localhost:16399/2')


class FakeCore:
    def __init__(self):
        self.artifacts: dict[str, dict] = {}
        self.lock = threading.Lock()
        self.record_reads: dict[str, int] = {}
        self.byte_reads: dict[str, int] = {}
        self.after_record_read = None  # callable(artifact_id, nth read), run as a record read is answered
        self.listing_enabled = True
        self._clock = datetime(2026, 10, 17, 9, 0, tzinfo=dt_timezone.utc)
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
                parts = url.path.strip('/').split('/')
                if parts == ['artifacts']:
                    return core._list(self, query)
                if len(parts) != 2 or parts[0] != 'artifacts':
                    return self._json(404, {'error': 'no route'})
                artifact_id = parts[1]
                with core.lock:
                    artifact = core.artifacts.get(artifact_id)
                    artifact = dict(artifact) if artifact else None
                if artifact is None:
                    return self._json(404, {'error': 'not found'})
                if query.get('record') == ['true']:
                    body = core._serialize(artifact_id, artifact)
                    with core.lock:
                        n = core.record_reads[artifact_id] = core.record_reads.get(artifact_id, 0) + 1
                    # Runs before the response is sent, so the change is in
                    # place before the reader can make its next request.
                    if core.after_record_read is not None:
                        core.after_record_read(artifact_id, n)
                    return self._json(200, body)
                with core.lock:
                    core.byte_reads[artifact_id] = core.byte_reads.get(artifact_id, 0) + 1
                if artifact['missing']:
                    return self._json(404, {'error': 'artifact file missing', 'path': artifact['path']})
                self.send_response(200)
                self.send_header('Content-Type', 'application/octet-stream')
                self.send_header('Content-Length', str(len(artifact['content'])))
                self.end_headers()
                self.wfile.write(artifact['content'])
                return None

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.server.server_address[1]}'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def _tick(self):
        self._clock += timedelta(seconds=1, microseconds=1)
        return self._clock

    @staticmethod
    def _serialize(artifact_id, artifact):
        return {'id': artifact_id, 'path': artifact['path'], 'originalFilename': artifact['originalFilename'],
                'uploadedBy': 'admin', 'createdAt': artifact['createdAt'].isoformat(),
                'updatedAt': artifact['updatedAt'].isoformat()}

    def _list(self, handler, query):
        if not self.listing_enabled:
            return handler._json(404, {'error': 'no route'})
        # Core's shape (services/core/artifacts/views.py list_artifacts):
        # keyset-paged in id order, the cursor being the last id served.
        limit = int(query.get('limit', ['1000'])[0])
        after = query.get('cursor', [''])[0]
        with self.lock:
            ids = [i for i in sorted(self.artifacts) if not after or i > after]
            page = ids[:limit]
            items = [{'id': i, 'updatedAt': self.artifacts[i]['updatedAt'].isoformat()} for i in page]
        return handler._json(200, {'artifacts': items, 'nextCursor': page[-1] if page else (after or None),
                                   'hasMore': len(ids) > limit})

    # --- What Core's admin and the volume do ------------------------------

    def upload(self, content: bytes, filename: str = '') -> str:
        artifact_id = str(uuid.uuid4())
        now = self._tick()
        with self.lock:
            self.artifacts[artifact_id] = {'content': content, 'originalFilename': filename, 'createdAt': now,
                                           'updatedAt': now, 'path': f'{artifact_id[:2]}/{artifact_id}',
                                           'missing': False}
        return artifact_id

    def reupload(self, artifact_id: str, content: bytes, filename: str | None = None) -> datetime:
        now = self._tick()
        with self.lock:
            artifact = self.artifacts[artifact_id]
            artifact['content'] = content
            artifact['updatedAt'] = now
            if filename is not None:
                artifact['originalFilename'] = filename
        return now

    def edit_in_place(self, artifact_id: str, content: bytes) -> None:
        with self.lock:
            self.artifacts[artifact_id]['content'] = content

    def write_bytes_only(self, artifact_id: str, content: bytes) -> None:
        """A re-upload's first half: the bytes land before the row commits
        (brief I-05)."""
        self.edit_in_place(artifact_id, content)

    def commit_row(self, artifact_id: str) -> datetime:
        now = self._tick()
        with self.lock:
            self.artifacts[artifact_id]['updatedAt'] = now
        return now

    def updated_at(self, artifact_id: str) -> datetime:
        with self.lock:
            return self.artifacts[artifact_id]['updatedAt']

    def content(self, artifact_id: str) -> bytes:
        with self.lock:
            return self.artifacts[artifact_id]['content']


# --- Upload events, in Core's envelope (services/core/artifacts/events.py) ---

def upload_event(artifact_id: str, action: str = 'created') -> dict:
    now = datetime.now(dt_timezone.utc).isoformat()
    envelope = {
        'schemaVersion': 1, 'messageId': f'msg-{uuid.uuid4()}', 'kind': 'artifact.event', 'project': '_instance',
        'taskId': None, 'contextId': None, 'correlationId': None, 'createdAt': now,
        'payload': {'eventType': 'artifact.uploaded', 'artifactId': artifact_id, 'path': f'x/{artifact_id}',
                    'action': action, 'actor': 'admin', 'uploadedAt': now},
    }
    return {'data': json.dumps(envelope)}


# --- Sample files, one per declared format ----------------------------------

def make_pdf(text: str) -> bytes:
    """A one-page PDF whose page draws ``text`` in Helvetica, with a correct
    cross-reference table."""
    stream = f'BT /F1 12 Tf 72 720 Td ({text}) Tj ET'.encode()
    objects = [
        b'<< /Type /Catalog /Pages 2 0 R >>',
        b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R '
        b'/Resources << /Font << /F1 5 0 R >> >> >>',
        b'<< /Length ' + str(len(stream)).encode() + b' >>\nstream\n' + stream + b'\nendstream',
        b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
    ]
    out = io.BytesIO()
    out.write(b'%PDF-1.4\n')
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f'{number} 0 obj\n'.encode() + body + b'\nendobj\n')
    xref = out.tell()
    out.write(f'xref\n0 {len(objects) + 1}\n0000000000 65535 f \n'.encode())
    for offset in offsets:
        out.write(f'{offset:010d} 00000 n \n'.encode())
    out.write(f'trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode())
    return out.getvalue()


def make_docx(*paragraphs: str) -> bytes:
    w = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
    body = ''.join(f'<w:p><w:r><w:t>{p}</w:t></w:r></w:p>' for p in paragraphs)
    document = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                f'<w:document xmlns:w="{w}"><w:body>{body}</w:body></w:document>')
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('[Content_Types].xml', '<?xml version="1.0"?><Types/>')
        archive.writestr('word/document.xml', document)
    return out.getvalue()


PNG = (b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4'
       b'\x89\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82')


def heavy_html(repeats: int = 120000) -> bytes:
    """An HTML file whose parse costs several CPU-seconds: the stand-in for
    a hostile file, for the crash and time-limit cases."""
    return b'<html><body>' + b'<p>word <b>bold</b> <i>it</i></p>' * repeats + b'</body></html>'


# --- Processes ---------------------------------------------------------------

def start_extractor(spool: Path, *extra: str) -> subprocess.Popen:
    """The extractor process, as its container runs it."""
    return subprocess.Popen([sys.executable, '-m', 'extractor', '--spool', str(spool), *extra], cwd=MEMORY_DIR,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.send_signal(signal.SIGKILL)
    process.wait(10)


def wait_for(predicate, seconds=20.0, interval=0.05):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


# --- Fixtures ----------------------------------------------------------------

@pytest.fixture
def core(settings):
    fake = FakeCore().start()
    settings.MEMORY_CORE_URL = fake.url
    yield fake
    fake.stop()


@pytest.fixture
def spool_dir(tmp_path, settings):
    root = tmp_path / 'spool'
    settings.MEMORY_EXTRACTION_SPOOL = str(root)
    settings.MEMORY_EXTRACTION_TIME_LIMIT_SECONDS = 20
    settings.MEMORY_EXTRACTION_GRACE_SECONDS = 5
    settings.MEMORY_EXTRACTION_ATTEMPT_LIMIT = 3
    settings.MEMORY_EXTRACTION_WORKER_ID = 'test-worker'
    return root


@pytest.fixture
def extractor(spool_dir):
    """The extractor process, running on the test spool for the whole test."""
    from extraction.spool import Spool
    Spool(spool_dir)
    process = start_extractor(spool_dir)
    yield process
    stop(process)


@pytest.fixture
def redis_events(settings):
    """The real stream name on Memory's own test Redis database, emptied."""
    import redis

    from extraction.events import STREAM
    client = redis.Redis.from_url(REDIS_TEST_URL, decode_responses=True)
    client.delete(STREAM)
    settings.MEMORY_REDIS_URL = REDIS_TEST_URL
    yield client
    client.delete(STREAM)  # and with it the consumer group


def drain(spool_root, worker='test-worker', limit=50):
    """Work the job store in this process until nothing is ready, the way
    ``run_extraction``'s loop does. Returns the end state of each job run."""
    from extraction import jobs
    from extraction.spool import Spool
    spool = Spool(spool_root)
    states = []
    for _ in range(limit):
        job = jobs.claim(worker)
        if job is None:
            break
        states.append(jobs.run(job, spool))
    return states


def process_env(settings, database_name: str, **extra) -> dict:
    """The environment a separate ``manage.py run_extraction`` process needs
    to work against this test's database, fake Core, spool and Redis."""
    env = {**os.environ,
           'PGDATABASE': database_name,
           'MEMORY_CORE_URL': settings.MEMORY_CORE_URL,
           'MEMORY_REDIS_URL': settings.MEMORY_REDIS_URL,
           'MEMORY_EXTRACTION_SPOOL': settings.MEMORY_EXTRACTION_SPOOL,
           'MEMORY_EXTRACTION_TIME_LIMIT_SECONDS': str(settings.MEMORY_EXTRACTION_TIME_LIMIT_SECONDS),
           'MEMORY_EXTRACTION_GRACE_SECONDS': str(settings.MEMORY_EXTRACTION_GRACE_SECONDS),
           'MEMORY_EXTRACTION_ATTEMPT_LIMIT': str(settings.MEMORY_EXTRACTION_ATTEMPT_LIMIT),
           'MEMORY_EXTRACTION_WORKER_ID': settings.MEMORY_EXTRACTION_WORKER_ID,
           'MEMORY_EXTRACTION_SWEEP_SECONDS': str(settings.MEMORY_EXTRACTION_SWEEP_SECONDS)}
    env.update({k: str(v) for k, v in extra.items()})
    return env


def start_worker(env: dict) -> subprocess.Popen:
    """``manage.py run_extraction``, as the memory-extraction container runs it."""
    # Its log goes to a file, never a pipe nobody drains (a full pipe would
    # block the worker); ``worker_log`` reads it for a failing assertion.
    log = tempfile.NamedTemporaryFile(prefix='run_extraction-', suffix='.log', delete=False)
    process = subprocess.Popen([sys.executable, 'manage.py', 'run_extraction'], cwd=MEMORY_DIR, env=env,
                               stdout=log, stderr=subprocess.STDOUT)
    process.log_path = log.name
    log.close()
    return process


def worker_log(process) -> str:
    try:
        return Path(process.log_path).read_text()[-4000:]
    except OSError:
        return ''
