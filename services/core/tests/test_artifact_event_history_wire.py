"""
Durable Artifact Event History REQ-03 — an announcement's wire shape and
message identity are unchanged.

Each announcement is published twice from one fixture: once by the
pre-change publisher (``tests/artifact_event_history_pre_change.py``, the
code at ``6077676`` frozen verbatim), straight to Redis as it used to be,
and once through the real post-change path — the admin view or the
librarian's consumer, then the outbox and the relay. The two envelopes are
diffed field by field: only the freshly minted ``messageId`` and
``createdAt``-class timestamps may differ, and every ``messageId`` has the
``msg-<uuid>`` form ``workitems.envelope.new_message_id`` mints.

Then ``setup/commons/tools/request-artifact.js``, unmodified, asks the real
librarian for an artifact through a real ``manage.py relay`` process and
must match its answer by ``correlationId`` and exit 0.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
from pathlib import Path

from django.db import connection

from artifacts.events import ARTIFACT_EVENT_STREAM
from librarian.envelope import REQUEST_KIND, build_envelope, to_stream_fields
from librarian.models import ArtifactDelivery
from librarian.stream_topology import REQUEST_STREAM, RESPONSE_STREAM
from workitems.models import OutboxEvent
from workitems.relay import relay_once

from tests import artifact_event_history_pre_change as pre_change
from tests.artifacts_support import (  # noqa: F401 - committed_artifact_env is a fixture
    MARKDOWN_BYTES, admin_client, change_url, committed_artifact_env, drain_outbox, post_upload, upload,
)
from tests.librarian_support import (  # noqa: F401 - librarian_env is a fixture
    await_response, files_in, last_response_id, librarian_env, make_repo, seed_artifact,
)

MOCKUP = b'\x89PNG\r\n\x1a\n-pretend-this-is-a-mockup-'
MESSAGE_ID = re.compile(r'^msg-[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$')

CORE_ROOT = Path(__file__).resolve().parent.parent
REQUEST_ARTIFACT_JS = CORE_ROOT.parent.parent / 'setup' / 'commons' / 'tools' / 'request-artifact.js'


def _entry(client, stream, entry_id) -> dict:
    (_id, fields), = client.xrange(stream, min=entry_id, max=entry_id)
    return json.loads(fields['data'])


def _last_entry(client, stream) -> dict:
    (_id, fields), = client.xrevrange(stream, count=1)
    return json.loads(fields['data'])


def _assert_same_but_identity_and_time(before: dict, after: dict, *, timestamps=('createdAt',),
                                       payload_timestamps=()) -> None:
    """Field by field, top level and payload: the same keys, and the same
    values everywhere except the fields named here."""
    assert set(before) == set(after), (sorted(before), sorted(after))
    for field in before:
        if field == 'messageId':
            assert MESSAGE_ID.match(before[field]) and MESSAGE_ID.match(after[field]), (before[field], after[field])
            continue
        if field in timestamps:
            continue
        if field == 'payload':
            assert set(before['payload']) == set(after['payload']), (sorted(before['payload']),
                                                                     sorted(after['payload']))
            for key in before['payload']:
                if key not in payload_timestamps:
                    assert before['payload'][key] == after['payload'][key], key
            continue
        assert before[field] == after[field], field


# --- the upload event ------------------------------------------------------

def test_the_upload_event_has_the_pre_change_shape_for_a_first_upload_and_a_replacement(committed_artifact_env):
    redis = committed_artifact_env.redis
    client = admin_client()

    artifact = upload(client, content=MARKDOWN_BYTES, filename='prd.md')
    drain_outbox(redis)
    created_after = _last_entry(redis, ARTIFACT_EVENT_STREAM)
    created_before = _entry(redis, ARTIFACT_EVENT_STREAM,
                            pre_change.publish_upload(artifact, created=True, client=redis))

    assert post_upload(client, content=b'# v2\n', filename='prd.md', url=change_url(artifact.id)).status_code == 302
    drain_outbox(redis)
    replaced_after = _last_entry(redis, ARTIFACT_EVENT_STREAM)
    artifact.refresh_from_db()
    replaced_before = _entry(redis, ARTIFACT_EVENT_STREAM,
                             pre_change.publish_upload(artifact, created=False, client=redis))

    # Same artifact row on both sides, so even createdAt (the upload's time)
    # agrees; only the minted messageId differs.
    _assert_same_but_identity_and_time(created_before, created_after, timestamps=())
    _assert_same_but_identity_and_time(replaced_before, replaced_after, timestamps=())
    assert created_after['payload']['action'] == 'created'
    assert replaced_after['payload']['action'] == 'replaced'
    assert created_after['messageId'] != created_before['messageId']


def test_a_relay_redo_keeps_the_stored_message_id_and_adds_no_entry(committed_artifact_env):
    redis = committed_artifact_env.redis
    upload(admin_client(), content=MARKDOWN_BYTES, filename='prd.md')
    drain_outbox(redis)
    row = OutboxEvent.objects.get(stream=ARTIFACT_EVENT_STREAM)
    message_id = row.envelope['messageId']

    OutboxEvent.objects.filter(id=row.id).update(published_at=None)
    assert relay_once(redis) == 1

    assert redis.get(f'aigang:idem:publish:outbox:{row.id}') == message_id
    entries = [json.loads(f['data']) for _id, f in redis.xrange(ARTIFACT_EVENT_STREAM)]
    assert [e['messageId'] for e in entries] == [message_id]
    assert OutboxEvent.objects.get(id=row.id).published_at is not None


# --- the librarian's two answers --------------------------------------------

def _reset_repository(repo: Path) -> None:
    for path in sorted(repo.rglob('*'), reverse=True):
        path.unlink() if path.is_file() else path.rmdir()
    ArtifactDelivery.objects.all().delete()


def _answer_through_the_consumer(env, request_envelope) -> dict:
    cursor = last_response_id(env.redis)
    env.redis.xadd(REQUEST_STREAM, to_stream_fields(request_envelope))
    return await_response(env, request_envelope['messageId'], cursor)


def test_a_delivery_confirmation_has_the_pre_change_shape(librarian_env):
    artifact = seed_artifact(MOCKUP)
    repo = make_repo(librarian_env, 'hello-web')
    request = build_envelope(REQUEST_KIND, task_id='task-77', payload={
        'requestedBy': 'frontend-agent', 'artifactId': str(artifact.id),
        'destinationRepo': 'hello-web', 'requestedPath': 'designs/mockup.png', 'taskId': 'task-77',
    })

    before = _entry(librarian_env.redis, RESPONSE_STREAM, pre_change.handle_request(librarian_env.redis, request))
    assert files_in(repo) == ['designs/mockup.png']
    _reset_repository(repo)
    after = _answer_through_the_consumer(librarian_env, request)

    assert before['payload']['status'] == after['payload']['status'] == 'delivered'
    assert before['correlationId'] == after['correlationId'] == request['messageId']
    # deliveredAt is when the record was first written: a fresh record on
    # each side after the reset, so a createdAt-class timestamp.
    _assert_same_but_identity_and_time(before, after, payload_timestamps=('deliveredAt',))


def test_a_delivery_failure_has_the_pre_change_shape(librarian_env):
    artifact = seed_artifact(MOCKUP)
    request = build_envelope(REQUEST_KIND, payload={
        'requestedBy': 'frontend-agent', 'artifactId': str(artifact.id),
        'destinationRepo': 'no-such-repo', 'requestedPath': 'designs/mockup.png',
    })

    before = _entry(librarian_env.redis, RESPONSE_STREAM, pre_change.handle_request(librarian_env.redis, request))
    after = _answer_through_the_consumer(librarian_env, request)

    assert before['payload']['status'] == after['payload']['status'] == 'failed'
    assert before['payload']['reason'] == after['payload']['reason'] == 'unknown_destination_repo'
    assert before['correlationId'] == after['correlationId'] == request['messageId']
    _assert_same_but_identity_and_time(before, after)


# --- request-artifact.js, unmodified, end to end ---------------------------

def _spawn_relay():
    env = dict(os.environ)
    env['PGDATABASE'] = connection.settings_dict['NAME']  # the test database this process committed to
    env.setdefault('REDIS_URL', os.environ.get('REDIS_TEST_URL', 'redis://localhost:16399'))
    return subprocess.Popen([sys.executable, str(CORE_ROOT / 'manage.py'), 'relay'], cwd=str(CORE_ROOT), env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def test_request_artifact_js_gets_its_answer_through_the_relay(librarian_env):
    artifact = seed_artifact(MOCKUP)
    repo = make_repo(librarian_env, 'hello-web')
    redis_url = os.environ.get('REDIS_TEST_URL', 'redis://localhost:16399')
    host, port = re.match(r'redis://([^:/]+):(\d+)', redis_url).groups()
    node_path = subprocess.run(['npm', 'root', '-g'], capture_output=True, text=True, check=True).stdout.strip()

    relay = _spawn_relay()
    try:
        helper = subprocess.run(
            ['node', str(REQUEST_ARTIFACT_JS), 'hello-web', str(artifact.id), 'designs/mockup.png'],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, 'REDIS_HOST': host, 'REDIS_PORT': port, 'NODE_PATH': node_path,
                 'AGENT_DISPLAY_NAME': 'frontend-agent'},
        )
    finally:
        relay.send_signal(signal.SIGKILL)
        relay.wait(timeout=10)

    assert helper.returncode == 0, helper.stderr
    assert helper.stdout.strip() == 'designs/mockup.png'
    assert files_in(repo) == ['designs/mockup.png']
    (_id, request_fields), = librarian_env.redis.xrange(REQUEST_STREAM)
    request = json.loads(request_fields['data'])
    answers = [json.loads(f['data']) for _id, f in librarian_env.redis.xrange(RESPONSE_STREAM)]
    assert [a['correlationId'] for a in answers] == [request['messageId']]
    assert answers[0]['payload']['path'] == 'designs/mockup.png'
