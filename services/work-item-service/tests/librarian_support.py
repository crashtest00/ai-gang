"""
Shared helpers for the librarian tests
(strategy/v4.0/features/librarian.md).

Not collected by pytest (pytest.ini's ``python_files = test_*.py``);
imported by the ``test_librarian_*.py`` modules. It lives here rather than
in the service-wide ``conftest.py``, which this track does not own.

**Everything runs through the real path.** ``librarian_env`` starts the
same consumer ``manage.py run_librarian`` starts, against the real test
Redis; ``request_delivery`` publishes a real envelope onto
``aigang:librarian:requests`` and reads the answer back off
``aigang:librarian:responses`` exactly the way ``librarian/README.md``
tells a requester to. Artifacts are seeded through artifact ingress's own
admin upload view (``tests/artifacts_support.py``), not by writing files
onto the volume behind its back. No test in this track calls
``delivery.deliver`` in place of any of that.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from redis.exceptions import ResponseError

from librarian.consumer import create_librarian_consumer
from librarian.envelope import REQUEST_KIND, build_envelope, to_stream_fields
from librarian.stream_topology import REQUEST_STREAM, RESPONSE_STREAM
from tests.artifacts_support import admin_client, upload

# Long enough that a slow container never trips it, short enough that a
# genuinely lost response fails the test rather than hanging the suite.
RESPONSE_TIMEOUT_SECONDS = 20


@pytest.fixture
def librarian_env(transactional_db, settings, tmp_path, redis_client):
    """Real Postgres, real Redis, a temporary artifact volume, a temporary
    projects root, and a running librarian subscriber.

    ``transactional_db`` rather than ``db``: the consumer runs on its own
    thread with its own database connection, so nothing it writes would be
    visible to (or rolled back with) a test-thread transaction.
    """
    artifact_root = tmp_path / 'artifact-root'
    artifact_root.mkdir()
    projects_root = tmp_path / 'projects'
    projects_root.mkdir()
    settings.ARTIFACT_ROOT = str(artifact_root)
    settings.PROJECTS_ROOT = str(projects_root)
    # The upload helper logs an admin in on every call and Django's default
    # hasher costs about a second each time. Nothing here tests hashing.
    settings.PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']

    env = SimpleNamespace(
        artifact_root=artifact_root, projects_root=projects_root, redis=redis_client, consumers=[],
    )
    start_consumer(env, 'librarian-test-1')
    try:
        yield env
    finally:
        for consumer in env.consumers:
            consumer.stop(timeout=5)


def start_consumer(env, name: str, *, block_ms: int = 5000):
    """Start another real subscriber in the same consumer group — what a
    second librarian process is, from Redis's point of view.

    ``block_ms`` is how long each ``XREADGROUP`` blocks. The concurrency
    test raises it so both subscribers are certainly *inside* a blocking
    read when its two requests are published, and Redis therefore hands
    one entry to each rather than both to whichever asked next.
    """
    def factory():
        import redis as redis_lib
        import os
        return redis_lib.Redis.from_url(os.environ['REDIS_TEST_URL'], decode_responses=True)

    consumer = create_librarian_consumer(factory, consumer_name=name)
    # One entry at a time, so two running consumers each take one of two
    # requests instead of the first one draining the batch.
    consumer.batch_size = 1
    consumer.block_ms = block_ms
    consumer.start()
    env.consumers.append(consumer)
    _await_group_member(env, name)
    return consumer


def _await_group_member(env, name: str) -> None:
    """Block until the consumer has joined the group, so a request
    published immediately after this call cannot be missed."""
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            names = {c['name'] for c in env.redis.xinfo_consumers(REQUEST_STREAM, 'librarian')}
            if name in names:
                return
        except ResponseError:
            pass
        time.sleep(0.02)
    raise AssertionError(f'consumer {name} never joined the group')


def make_repo(env, name: str, files: dict | None = None) -> Path:
    """A project repository: one directory directly under the projects
    root, which is what `destination_repo` names."""
    repo = env.projects_root / name
    repo.mkdir(parents=True, exist_ok=True)
    for relative, content in (files or {}).items():
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content if isinstance(content, bytes) else content.encode())
    return repo


def seed_artifact(content: bytes, filename: str = 'mockup.png', username: str = 'product-owner'):
    """Register an artifact through artifact ingress's real upload path."""
    return upload(admin_client(username), content=content, filename=filename)


def last_response_id(client) -> str:
    """The response stream's current last entry id — what a requester
    records BEFORE publishing, so it cannot miss an answer that arrives
    between the publish and the read (librarian/README.md)."""
    try:
        return client.xinfo_stream(RESPONSE_STREAM)['last-generated-id']
    except ResponseError:
        return '0-0'


def publish_request(env, payload: dict, *, task_id=None) -> tuple[str, str]:
    """Publish one request. Returns (messageId, the response-stream id to
    read from), which is the pair the README's contract is built on."""
    cursor = last_response_id(env.redis)
    envelope = build_envelope(REQUEST_KIND, payload=payload, task_id=task_id)
    env.redis.xadd(REQUEST_STREAM, to_stream_fields(envelope))
    return envelope['messageId'], cursor


def await_response(env, message_id: str, cursor: str, timeout: float = RESPONSE_TIMEOUT_SECONDS) -> dict:
    """Block for the one response correlated to ``message_id``."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        response = env.redis.xread({RESPONSE_STREAM: cursor}, count=20, block=250)
        for _stream, entries in response or []:
            for entry_id, fields in entries:
                cursor = entry_id
                envelope = json.loads(fields['data'])
                if envelope.get('correlationId') == message_id:
                    return envelope
    raise AssertionError(f'no response correlated to {message_id} within {timeout}s')


def request_delivery(env, *, artifact_id=None, destination_repo=None, requested_path=None,
                     requested_by='frontend-agent', task_id=None, payload=None) -> dict:
    """Publish a delivery request and return its single response envelope.

    ``payload`` overrides the whole payload, for the tests that need a
    request with a field missing or spelled the other way.
    """
    if payload is None:
        payload = {
            'requestedBy': requested_by,
            'artifactId': str(artifact_id),
            'destinationRepo': destination_repo,
            'requestedPath': requested_path,
        }
        if task_id is not None:
            payload['taskId'] = task_id
    message_id, cursor = publish_request(env, payload, task_id=task_id)
    return await_response(env, message_id, cursor)


def all_responses(env) -> list:
    return [json.loads(fields['data']) for _entry_id, fields in env.redis.xrange(RESPONSE_STREAM)]


def files_in(repo: Path) -> list:
    return sorted(p.relative_to(repo).as_posix() for p in repo.rglob('*') if p.is_file())
