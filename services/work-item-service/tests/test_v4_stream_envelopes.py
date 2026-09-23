"""
The three V4 stream kinds are the platform envelope's kinds.

V4 audit Pass 1 row 15. ``workitems.envelope.VALID_KINDS`` was a closed
set of work-item and Jira kinds, so artifact ingress published with a raw
``XADD`` instead of ``workitems.streams.publish`` (no envelope validation,
no dedupe) and the librarian carried a private kind set with a consumer
that overrode the shared decode. Both are gone; what is left to prove is
the property that made them necessary, read off the real streams:

    an entry published by either V4 app is accepted by
    ``workitems.envelope.from_stream_fields``

which is the decode every consumer in this service uses, including the
librarian's own.
"""

from __future__ import annotations

from librarian.stream_topology import REQUEST_STREAM, RESPONSE_STREAM
from workitems.envelope import Kind, from_stream_fields
from artifacts.events import ARTIFACT_EVENT_STREAM
from tests.artifacts_support import (  # noqa: F401 - committed_artifact_env is a fixture
    MARKDOWN_BYTES, admin_client, committed_artifact_env, upload,
)
from tests.librarian_support import (  # noqa: F401 - librarian_env is a fixture
    librarian_env, make_repo, request_delivery, seed_artifact,
)

MOCKUP = b'\x89PNG\r\n\x1a\n-pretend-this-is-a-mockup-'


def _sole_envelope(client, stream: str) -> dict:
    """The one entry on a stream, decoded with the SHARED envelope — the
    decode `workitems.streams.Consumer` applies to every entry it reads."""
    entries = client.xrange(stream)
    assert len(entries) == 1, f'{stream}: expected one entry, got {len(entries)}'
    envelope = from_stream_fields(entries[0][1])
    assert envelope is not None, f'{stream}: the shared envelope rejected the entry {entries[0][1]!r}'
    return envelope


def test_an_artifact_upload_event_is_accepted_by_the_shared_envelope(committed_artifact_env):
    artifact = upload(admin_client(), content=MARKDOWN_BYTES, filename='prd.md')

    envelope = _sole_envelope(committed_artifact_env.redis, ARTIFACT_EVENT_STREAM)

    assert envelope['kind'] == Kind.ARTIFACT_EVENT
    assert envelope['payload']['artifactId'] == str(artifact.id)


def test_a_delivery_request_and_its_response_are_accepted_by_the_shared_envelope(librarian_env):
    artifact = seed_artifact(MOCKUP)
    make_repo(librarian_env, 'hello-web')

    response = request_delivery(librarian_env, artifact_id=artifact.id, destination_repo='hello-web',
                                requested_path='designs/mockup.png')
    assert response['payload']['status'] == 'delivered'

    request_envelope = _sole_envelope(librarian_env.redis, REQUEST_STREAM)
    response_envelope = _sole_envelope(librarian_env.redis, RESPONSE_STREAM)

    assert request_envelope['kind'] == Kind.ARTIFACT_DELIVERY_REQUEST
    assert response_envelope['kind'] == Kind.ARTIFACT_DELIVERY_RESPONSE
    assert response_envelope['correlationId'] == request_envelope['messageId']
