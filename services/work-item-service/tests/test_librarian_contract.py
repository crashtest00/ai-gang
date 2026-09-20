"""
REQ-01 — requests arrive as Streams messages, with the field contract.

Every test here publishes a real envelope onto the real
``aigang:librarian:requests`` stream in the test Redis and reads the
answer off the real response stream. Nothing calls the consumer's handler
directly.
"""

from __future__ import annotations

import json

import pytest

from librarian.envelope import REQUEST_KIND, RESPONSE_KIND, build_envelope, to_stream_fields
from librarian.failures import MISSING_FIELD
from librarian.responses import STATUS_DELIVERED, STATUS_FAILED
from librarian.stream_topology import REQUEST_GROUP, REQUEST_STREAM, RESPONSE_STREAM
from tests.librarian_support import (  # noqa: F401 - librarian_env is a fixture
    await_response, librarian_env, make_repo, publish_request, request_delivery, seed_artifact,
)

MOCKUP = b'\x89PNG\r\n\x1a\n-pretend-this-is-a-mockup-'


def test_the_librarian_owns_its_own_streams():
    """The stream is the librarian's own: not ScrumMaster's gateway (whose
    consumer group dead-letters kinds it does not know) and not the
    work-item command channel."""
    assert REQUEST_STREAM == 'aigang:librarian:requests'
    assert RESPONSE_STREAM == 'aigang:librarian:responses'
    assert REQUEST_GROUP == 'librarian'
    for stream in (REQUEST_STREAM, RESPONSE_STREAM):
        assert 'gateway' not in stream
        assert 'workitems' not in stream
        assert '{' not in stream


def test_a_request_with_every_field_is_processed(librarian_env):
    artifact = seed_artifact(MOCKUP)
    make_repo(librarian_env, 'hello-web')

    response = request_delivery(
        librarian_env, artifact_id=artifact.id, destination_repo='hello-web',
        requested_path='src/designs/mockup.png', requested_by='frontend-agent', task_id='task-77',
    )

    assert response['kind'] == RESPONSE_KIND
    assert response['payload']['status'] == STATUS_DELIVERED
    assert response['payload']['requestedBy'] == 'frontend-agent'
    assert response['payload']['artifactId'] == str(artifact.id)
    assert response['payload']['destinationRepo'] == 'hello-web'
    assert response['payload']['taskId'] == 'task-77'
    assert response['taskId'] == 'task-77'


def test_task_id_is_optional(librarian_env):
    artifact = seed_artifact(MOCKUP)
    make_repo(librarian_env, 'hello-web')

    response = request_delivery(librarian_env, artifact_id=artifact.id, destination_repo='hello-web',
                                requested_path='mockup.png')

    assert response['payload']['status'] == STATUS_DELIVERED
    assert response['payload']['taskId'] is None


@pytest.mark.parametrize('omitted', ['requestedBy', 'artifactId', 'destinationRepo', 'requestedPath'])
def test_a_request_missing_a_required_field_is_answered_with_that_field(librarian_env, omitted):
    """REQ-01's acceptance: "a request missing a required field is answered
    with a failure naming the missing field"."""
    artifact = seed_artifact(MOCKUP)
    make_repo(librarian_env, 'hello-web')
    payload = {
        'requestedBy': 'frontend-agent',
        'artifactId': str(artifact.id),
        'destinationRepo': 'hello-web',
        'requestedPath': 'mockup.png',
    }
    del payload[omitted]

    response = request_delivery(librarian_env, payload=payload)

    snake = {'requestedBy': 'requested_by', 'artifactId': 'artifact_id',
             'destinationRepo': 'destination_repo', 'requestedPath': 'requested_path'}[omitted]
    assert response['payload']['status'] == STATUS_FAILED
    assert response['payload']['reason'] == MISSING_FIELD
    assert snake in response['payload']['detail']


def test_the_specifications_own_snake_case_field_names_are_accepted(librarian_env):
    """REQ-01 names the fields ``requested_by``, ``artifact_id``,
    ``destination_repo``, ``requested_path``, ``task_id``. A requester
    written straight from those words works."""
    artifact = seed_artifact(MOCKUP)
    make_repo(librarian_env, 'hello-web')

    response = request_delivery(librarian_env, payload={
        'requested_by': 'refinement-agent',
        'artifact_id': str(artifact.id),
        'destination_repo': 'hello-web',
        'requested_path': 'designs/mockup.png',
        'task_id': 'task-12',
    })

    assert response['payload']['status'] == STATUS_DELIVERED
    assert response['payload']['path'] == 'designs/mockup.png'
    assert response['payload']['requestedBy'] == 'refinement-agent'
    assert response['payload']['taskId'] == 'task-12'


def test_the_response_correlates_to_the_requests_message_id(librarian_env):
    """The contract an agent-facing helper is written against: publish,
    then read the response stream from the id you recorded first, matching
    ``correlationId`` against the ``messageId`` you minted."""
    artifact = seed_artifact(MOCKUP)
    make_repo(librarian_env, 'hello-web')

    first_id, first_cursor = publish_request(librarian_env, {
        'requestedBy': 'a', 'artifactId': str(artifact.id),
        'destinationRepo': 'hello-web', 'requestedPath': 'a.png',
    })
    second_id, second_cursor = publish_request(librarian_env, {
        'requestedBy': 'b', 'artifactId': str(artifact.id),
        'destinationRepo': 'hello-web', 'requestedPath': 'b.png',
    })

    first = await_response(librarian_env, first_id, first_cursor)
    second = await_response(librarian_env, second_id, second_cursor)

    assert first['correlationId'] == first_id
    assert second['correlationId'] == second_id
    assert first['payload']['requestedBy'] == 'a'
    assert second['payload']['requestedBy'] == 'b'


def test_an_entry_that_is_not_a_librarian_envelope_is_dead_lettered_and_not_answered(librarian_env):
    """The ack/retry/dead-letter convention ``redis-streams.md``
    establishes, kept for the one case it fits: an entry this malformed
    names no requester, so there is nobody to answer."""
    librarian_env.redis.xadd(REQUEST_STREAM, {'data': 'not json at all'})

    dead = _await_dead_letter(librarian_env)
    assert dead['reason'] == 'invalid_envelope'
    assert librarian_env.redis.xlen(RESPONSE_STREAM) == 0


def test_a_response_replayed_onto_the_request_stream_is_dead_lettered(librarian_env):
    """A well-formed envelope of the wrong kind is refused rather than
    treated as a request."""
    envelope = build_envelope(RESPONSE_KIND, payload={'status': 'delivered'})
    librarian_env.redis.xadd(REQUEST_STREAM, to_stream_fields(envelope))

    dead = _await_dead_letter(librarian_env)
    assert 'not a request' in dead['reason']
    assert librarian_env.redis.xlen(RESPONSE_STREAM) == 0


def _await_dead_letter(env, timeout: float = 15) -> dict:
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        entries = env.redis.xrange(f'{REQUEST_STREAM}:dead')
        if entries:
            return json.loads(entries[0][1]['data'])
        time.sleep(0.05)
    raise AssertionError('nothing reached the dead-letter stream')
