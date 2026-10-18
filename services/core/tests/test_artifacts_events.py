"""
REQ-06 — a successful upload publishes one event.

Every assertion reads the REAL Redis stream (`aigang:artifacts:events`)
with XRANGE against the test Redis container; nothing stubs the publisher.
The one test that needs Redis to fail points the publisher's client at a
closed port rather than replacing it with a double, so the failure comes
from the same client library the deployment uses.
"""

from __future__ import annotations

import socket

import pytest
import redis
from django.test import Client

from artifacts.events import (
    ACTION_CREATED, ACTION_REPLACED, ARTIFACT_EVENT_KIND, ARTIFACT_EVENT_STREAM, ARTIFACT_UPLOADED, INSTANCE_SCOPE,
)
from artifacts.models import Artifact
from tests.artifacts_support import (  # noqa: F401 - committed_artifact_env is a fixture
    ADD_URL, MARKDOWN_BYTES, PNG_BYTES, admin_client, committed_artifact_env, change_url, post_upload, read_events,
    retrieve_url, stored_path, upload,
)


def test_the_stream_is_instance_wide_and_not_under_the_workitems_namespace():
    assert ARTIFACT_EVENT_STREAM == 'aigang:artifacts:events'
    assert 'workitems' not in ARTIFACT_EVENT_STREAM
    assert '{' not in ARTIFACT_EVENT_STREAM


def test_upload_publishes_one_created_event_with_the_stated_fields(committed_artifact_env):
    artifact = upload(admin_client('uploader'), content=MARKDOWN_BYTES, filename='prd.md')

    events = read_events(committed_artifact_env)

    assert len(events) == 1
    envelope = events[0]
    assert envelope['kind'] == ARTIFACT_EVENT_KIND
    assert envelope['project'] == INSTANCE_SCOPE
    assert envelope['schemaVersion'] == '1'
    assert envelope['messageId'].startswith('msg-')
    assert envelope['payload'] == {
        'eventType': ARTIFACT_UPLOADED,
        'artifactId': str(artifact.id),
        'path': artifact.path,
        'action': ACTION_CREATED,
        'actor': 'uploader',
        'uploadedAt': artifact.updated_at.isoformat(),
    }


def test_reupload_publishes_a_replaced_event_for_the_same_id_and_path(committed_artifact_env):
    client = admin_client()
    artifact = upload(client, content=MARKDOWN_BYTES, filename='prd.md')

    assert post_upload(client, content=b'# v2\n', filename='prd.md',
                       url=change_url(artifact.id)).status_code == 302

    events = read_events(committed_artifact_env)
    assert [e['payload']['action'] for e in events] == [ACTION_CREATED, ACTION_REPLACED]
    assert {e['payload']['artifactId'] for e in events} == {str(artifact.id)}
    assert {e['payload']['path'] for e in events} == {artifact.path}


def test_an_in_place_edit_publishes_nothing(committed_artifact_env):
    """REQ-06's asymmetry, stated as a property of the design: the stream
    records arrivals, not every change."""
    artifact = upload(admin_client(), content=MARKDOWN_BYTES, filename='prd.md')
    assert len(read_events(committed_artifact_env)) == 1

    stored_path(committed_artifact_env, artifact).write_bytes(b'# edited on the host\n')
    assert Client().get(retrieve_url(artifact.id)).status_code == 200

    assert len(read_events(committed_artifact_env)) == 1


def test_a_read_publishes_nothing(committed_artifact_env):
    artifact = upload(admin_client(), content=MARKDOWN_BYTES, filename='prd.md')

    for _ in range(3):
        Client().get(retrieve_url(artifact.id))
        Client().get(retrieve_url(artifact.id), {'record': 'true'})

    assert len(read_events(committed_artifact_env)) == 1


def test_an_admin_edit_that_brings_no_new_bytes_publishes_nothing(committed_artifact_env):
    client = admin_client()
    artifact = upload(client, content=MARKDOWN_BYTES, filename='prd.md')

    assert client.post(change_url(artifact.id), data={'original_filename': 'renamed.md'}).status_code == 302

    assert len(read_events(committed_artifact_env)) == 1


def test_each_upload_publishes_exactly_one_event(committed_artifact_env):
    client = admin_client()

    first = upload(client, content=MARKDOWN_BYTES, filename='a.md')
    second = upload(client, content=PNG_BYTES, filename='b.png')

    events = read_events(committed_artifact_env)
    assert [e['payload']['artifactId'] for e in events] == [str(first.id), str(second.id)]
    assert len({e['messageId'] for e in events}) == 2


def test_a_refused_upload_publishes_nothing(committed_artifact_env):
    """A POST the admin rejects (no file at all) leaves the stream empty —
    the event announces an arrival, and nothing arrived."""
    client = admin_client()

    response = client.post('/django-admin/artifacts/artifact/add/', data={'original_filename': 'nothing.md'})

    assert response.status_code == 200  # re-rendered form with errors, not a redirect
    assert read_events(committed_artifact_env) == []


def test_an_anonymous_upload_attempt_publishes_nothing(committed_artifact_env):
    post_upload(Client(), content=MARKDOWN_BYTES, filename='sneaked-in.md')

    assert read_events(committed_artifact_env) == []


# --- REQ-06 when Redis cannot take the event -----------------------------

def _a_closed_port() -> int:
    """A port nothing is listening on: bound, read back, and released."""
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


def _redis_that_is_down():
    return redis.Redis.from_url(f'redis://127.0.0.1:{_a_closed_port()}/0', decode_responses=True,
                                socket_connect_timeout=2)


def test_an_upload_made_while_redis_is_down_is_announced_once_redis_is_back(committed_artifact_env, monkeypatch):
    """durable-artifact-event-history.md REQ-01's Acceptance, replacing the
    test of the logged loss it removes.

    "Redis stopped" is a client pointed at a closed port, so the failure
    comes from the same client library the deployment uses without stopping
    the shared test container: every Redis client this service builds
    (``workitems.redis_client``) is pointed there while the upload runs, and
    the relay is first run against it too.

    The upload goes through the real admin add view. It succeeds without a
    server error, the ``artifact`` row exists, and exactly one outbox row
    for it is waiting with ``published_at IS NULL``. A relay pass against
    the stopped Redis fails and leaves it waiting. With Redis back, the
    relay publishes it, and the stream carries exactly one entry.
    """
    from workitems import redis_client as redis_client_module
    from workitems.models import OutboxEvent
    from workitems.relay import relay_once

    monkeypatch.setattr(redis_client_module, 'get_client', _redis_that_is_down)
    monkeypatch.setattr(redis_client_module, 'new_client', _redis_that_is_down)

    response = post_upload(admin_client(), content=MARKDOWN_BYTES, filename='prd.md', url=ADD_URL)

    assert response.status_code == 302
    artifact = Artifact.objects.get()
    assert artifact.file.name
    rows = list(OutboxEvent.objects.filter(stream=ARTIFACT_EVENT_STREAM))
    assert len(rows) == 1
    assert rows[0].published_at is None
    assert rows[0].envelope['payload']['artifactId'] == str(artifact.id)

    with pytest.raises(redis.exceptions.ConnectionError):
        relay_once(_redis_that_is_down())
    assert OutboxEvent.objects.get(id=rows[0].id).published_at is None
    assert committed_artifact_env.redis.xlen(ARTIFACT_EVENT_STREAM) == 0

    published = read_events(committed_artifact_env)

    assert len(published) == 1
    assert published[0]['payload']['artifactId'] == str(artifact.id)
    assert published[0]['payload']['action'] == ACTION_CREATED
    assert OutboxEvent.objects.get(id=rows[0].id).published_at is not None
