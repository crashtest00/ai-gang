"""
Durable Artifact Event History §4, "Deployment" — the librarian switch
procedure in ``librarian/README.md`` ("Switching from a librarian that
published its answers directly"), verified as §5 describes.

Under a real pre-change librarian process
(``tests/artifact_event_history_pre_change_librarian.py``), one request is
left unanswered by killing the process while its handling is held inside
``deliver``'s lock. A second request is left in the state a kill between the
pre-change handler's publish and its ``XACK`` leaves — read by the group, not
acknowledged, and answered on the response stream — built by hand because
that window is too short to hit with a kill. Then the README's steps 2 and 3
are followed exactly, the new librarian is started, and after its reclaim
each request has exactly one answer and nothing is pending.

``redis-cli`` is not installed on the build host, so the steps' Redis
commands are issued through redis-py, one for one.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from django.db import connection

from librarian.envelope import REQUEST_KIND, RESPONSE_KIND, build_envelope, to_stream_fields
from librarian.stream_topology import REQUEST_GROUP, REQUEST_STREAM, RESPONSE_STREAM
from workitems.streams import ensure_group

from tests.librarian_support import await_lock_granted, drain_outbox, files_in, make_repo, seed_artifact, start_consumer

CORE_ROOT = Path(__file__).resolve().parent.parent
PRE_CHANGE_LIBRARIAN = Path(__file__).resolve().parent / 'artifact_event_history_pre_change_librarian.py'
MOCKUP = b'\x89PNG\r\n\x1a\n-pretend-this-is-a-mockup-'


@pytest.fixture
def switch_env(transactional_db, settings, tmp_path, redis_client):
    artifact_root = tmp_path / 'artifact-root'
    artifact_root.mkdir()
    projects_root = tmp_path / 'projects'
    projects_root.mkdir()
    settings.ARTIFACT_ROOT = str(artifact_root)
    settings.PROJECTS_ROOT = str(projects_root)
    settings.PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']
    env = SimpleNamespace(artifact_root=artifact_root, projects_root=projects_root, redis=redis_client,
                          consumers=[])
    try:
        yield env
    finally:
        for consumer in env.consumers:
            consumer.stop(timeout=5)


def _until(predicate, *, timeout=20.0, what='the condition'):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(f'timed out waiting for {what}')


def _spawn_pre_change_librarian(env, name):
    process_env = dict(os.environ)
    process_env.update({
        'PGDATABASE': connection.settings_dict['NAME'],
        'REDIS_URL': os.environ.get('REDIS_TEST_URL', 'redis://localhost:16399'),
        'ARTIFACT_ROOT': str(env.artifact_root),
        'PROJECTS_ROOT': str(env.projects_root),
        'LIBRARIAN_LOCK_HOLD_DELAY_MS': '60000',
    })
    return subprocess.Popen([sys.executable, str(PRE_CHANGE_LIBRARIAN), name], cwd=str(CORE_ROOT), env=process_env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _request(artifact_id, path):
    return build_envelope(REQUEST_KIND, payload={
        'requestedBy': 'frontend-agent', 'artifactId': str(artifact_id),
        'destinationRepo': 'hello-web', 'requestedPath': path,
    })


def _follow_the_switch_procedure(redis) -> list:
    """``librarian/README.md``'s steps 2 and 3, command for command.
    Returns the entry ids it acknowledged."""
    # Step 2: XPENDING aigang:librarian:requests librarian - + <count>,
    # <count> at least the summary form's total.
    total = redis.xpending(REQUEST_STREAM, REQUEST_GROUP)['pending']
    pending = redis.xpending_range(REQUEST_STREAM, REQUEST_GROUP, min='-', max='+', count=max(total, 1))
    acknowledged = []
    for entry in pending:
        entry_id = entry['message_id']
        # Step 3: XRANGE the request, take its messageId, look for a
        # response whose correlationId equals it, XACK only then.
        (_id, fields), = redis.xrange(REQUEST_STREAM, min=entry_id, max=entry_id)
        message_id = json.loads(fields['data'])['messageId']
        answered = any(json.loads(f['data']).get('correlationId') == message_id
                       for _rid, f in redis.xrange(RESPONSE_STREAM, '-', '+'))
        if answered:
            redis.xack(REQUEST_STREAM, REQUEST_GROUP, entry_id)
            acknowledged.append(entry_id)
    return acknowledged


def test_the_switch_procedure_leaves_every_request_with_exactly_one_answer(switch_env):
    env = switch_env
    redis = env.redis
    artifact = seed_artifact(MOCKUP)
    repo = make_repo(env, 'hello-web')
    ensure_group(redis, REQUEST_STREAM, REQUEST_GROUP)

    # A request the pre-change librarian is killed in the middle of.
    old = _spawn_pre_change_librarian(env, 'pre-change-librarian')
    try:
        _until(lambda: any(c['name'] == 'pre-change-librarian'
                           for c in redis.xinfo_consumers(REQUEST_STREAM, REQUEST_GROUP)),
               what='the pre-change librarian to join the group')
        unanswered = _request(artifact.id, 'designs/unanswered.png')
        redis.xadd(REQUEST_STREAM, to_stream_fields(unanswered))
        await_lock_granted(str(artifact.id), 'hello-web', timeout=20)
    finally:
        old.send_signal(signal.SIGKILL)
        old.wait(timeout=10)

    # A request answered by the pre-change handler but not acknowledged.
    answered = _request(artifact.id, 'designs/answered.png')
    answered_entry = redis.xadd(REQUEST_STREAM, to_stream_fields(answered))
    read = redis.xreadgroup(REQUEST_GROUP, 'pre-change-librarian', {REQUEST_STREAM: '>'}, count=1)
    assert read[0][1][0][0] == answered_entry
    redis.xadd(RESPONSE_STREAM, to_stream_fields(build_envelope(RESPONSE_KIND, payload={
        'status': 'delivered', 'artifactId': str(artifact.id), 'destinationRepo': 'hello-web',
        'requestedBy': 'frontend-agent', 'taskId': None, 'path': 'designs/answered.png', 'action': 'copied',
        'deliveredAt': '2026-10-12T00:00:00+00:00',
    }, correlation_id=answered['messageId'])))

    assert redis.xpending(REQUEST_STREAM, REQUEST_GROUP)['pending'] == 2
    assert files_in(repo) == []

    assert _follow_the_switch_procedure(redis) == [answered_entry]

    new = start_consumer(env, 'new-librarian')
    new.retry_delay_ms = 300  # stands in for DEFAULT_RETRY_DELAY_MS
    new.reclaim_interval_ms = 250
    _until(lambda: redis.xpending(REQUEST_STREAM, REQUEST_GROUP)['pending'] == 0, timeout=30,
           what='the new librarian to reclaim and answer the unanswered request')

    drain_outbox(redis)
    answers = [json.loads(f['data']) for _id, f in redis.xrange(RESPONSE_STREAM)]
    by_request = {m: [a for a in answers if a['correlationId'] == m]
                  for m in (unanswered['messageId'], answered['messageId'])}
    assert {m: len(a) for m, a in by_request.items()} == {unanswered['messageId']: 1, answered['messageId']: 1}
    assert by_request[unanswered['messageId']][0]['payload']['status'] == 'delivered'
    assert redis.xpending(REQUEST_STREAM, REQUEST_GROUP)['pending'] == 0
