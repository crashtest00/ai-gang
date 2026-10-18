"""
Durable Artifact Event History REQ-05 — every announcement is published at
least once, and announced once, across a Redis outage and relay restarts.

The relay runs as real ``python manage.py relay`` processes against the
test database, as in ``test_relay_integration.py``. First it runs with its
``published_at`` update made to fail once per row after ``publish`` has
returned (``tests/artifact_event_history_relay_fail_once.py``) — the state
a crash between its ``XADD`` and that update leaves — and is restarted
until every row has failed once; then it runs unmodified.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import redis as redis_lib
from django.db import connection, transaction

from artifacts.events import ARTIFACT_EVENT_STREAM
from librarian.envelope import REQUEST_KIND, build_envelope, to_stream_fields
from librarian.stream_topology import REQUEST_GROUP, REQUEST_STREAM, RESPONSE_STREAM
from workitems import redis_client as redis_client_module
from workitems import store
from workitems.models import OutboxEvent
from workitems.stream_topology import event_stream_name

from tests.librarian_support import librarian_env, make_repo, seed_artifact  # noqa: F401 - a fixture

CORE_ROOT = Path(__file__).resolve().parent.parent
FAIL_ONCE = Path(__file__).resolve().parent / 'artifact_event_history_relay_fail_once.py'
PROJECT = 'relay-restart-project'
MOCKUP = b'\x89PNG\r\n\x1a\n-pretend-this-is-a-mockup-'


def _relay_env():
    env = dict(os.environ)
    env['PGDATABASE'] = connection.settings_dict['NAME']  # the test database this process committed to
    env.setdefault('REDIS_URL', os.environ.get('REDIS_TEST_URL', 'redis://localhost:16399'))
    env['RELAY_POLL_INTERVAL_MS'] = '100'
    return env


def _redis_that_is_down():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    return redis_lib.Redis.from_url(f'redis://127.0.0.1:{port}/0', decode_responses=True, socket_connect_timeout=2)


def _until(predicate, *, timeout=20.0, what='the condition'):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(f'timed out waiting for {what}')


def test_req05_each_announcement_appears_once_after_updates_that_fail_once_per_row(librarian_env, monkeypatch,
                                                                                   tmp_path):
    redis = librarian_env.redis

    # Redis stopped: the upload commits its announcement row regardless.
    monkeypatch.setattr(redis_client_module, 'get_client', _redis_that_is_down)
    monkeypatch.setattr(redis_client_module, 'new_client', _redis_that_is_down)
    artifact = seed_artifact(MOCKUP)
    monkeypatch.undo()

    # Redis back, relay still stopped: the delivery answer waits as a row too.
    make_repo(librarian_env, 'hello-web')
    request = build_envelope(REQUEST_KIND, payload={
        'requestedBy': 'frontend-agent', 'artifactId': str(artifact.id),
        'destinationRepo': 'hello-web', 'requestedPath': 'designs/mockup.png',
    })
    redis.xadd(REQUEST_STREAM, to_stream_fields(request))
    _until(lambda: OutboxEvent.objects.filter(stream=RESPONSE_STREAM).exists()
           and redis.xpending(REQUEST_STREAM, REQUEST_GROUP)['pending'] == 0, what='the committed answer row')
    with transaction.atomic():
        store.write_outbox_event(project=PROJECT, event_type='work_item.status_changed', work_item_id=uuid.uuid4(),
                                 payload={'to': 'ready'})

    rows = list(OutboxEvent.objects.order_by('created_at'))
    assert [r.stream for r in rows] == [ARTIFACT_EVENT_STREAM, RESPONSE_STREAM, None]
    assert all(r.published_at is None for r in rows)
    assert redis.xlen(ARTIFACT_EVENT_STREAM) == 0 and redis.xlen(RESPONSE_STREAM) == 0

    failed_rows = tmp_path / 'failed-rows'
    restarts = 0
    while len(set(failed_rows.read_text().split()) if failed_rows.exists() else ()) < len(rows):
        restarts += 1
        assert restarts <= len(rows), 'each run fails exactly one new row'
        modified = subprocess.Popen([sys.executable, str(FAIL_ONCE), str(failed_rows)], cwd=str(CORE_ROOT),
                                    env=_relay_env(), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            _stdout, stderr = modified.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            modified.kill()
            raise
        assert modified.returncode != 0
        assert b'failed after publish' in stderr, stderr.decode()[-2000:]
    assert restarts == len(rows)

    unmodified = subprocess.Popen([sys.executable, str(CORE_ROOT / 'manage.py'), 'relay'], cwd=str(CORE_ROOT),
                                  env=_relay_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        _until(lambda: not OutboxEvent.objects.filter(published_at__isnull=True).exists(),
               what='the unmodified relay to mark every row published')
    finally:
        unmodified.send_signal(signal.SIGKILL)
        unmodified.wait(timeout=10)

    for row in rows:
        row.refresh_from_db()
        assert row.published_at is not None
        # Every row's redo was deduplicated, so it keeps stream_entry_id NULL.
        assert row.stream_entry_id is None
        message_id = row.envelope['messageId'] if row.envelope else str(row.id)
        assert redis.get(f'aigang:idem:publish:outbox:{row.id}') == message_id

    for stream, row in ((ARTIFACT_EVENT_STREAM, rows[0]), (RESPONSE_STREAM, rows[1]),
                        (event_stream_name(PROJECT), rows[2])):
        published = [json.loads(f['data']) for _id, f in redis.xrange(stream)]
        expected = row.envelope['messageId'] if row.envelope else str(row.id)
        assert [e['messageId'] for e in published] == [expected], stream
    assert json.loads(redis.xrange(RESPONSE_STREAM)[0][1]['data'])['correlationId'] == request['messageId']
