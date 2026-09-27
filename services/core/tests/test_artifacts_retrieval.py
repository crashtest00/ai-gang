"""
REQ-02 (every read resolves against the file on the volume) and REQ-03
(retrieval by canonical id as a direct synchronous read, access-logged).

Retrieval is exercised through the real URL, not by calling the view
function or the read store directly.
"""

from __future__ import annotations

import json

from django.test import Client

from artifacts.models import ArtifactAccessLog
from tests.artifacts_support import (  # noqa: F401 - artifact_env is a fixture
    JAVASCRIPT_BYTES, MARKDOWN_BYTES, PNG_BYTES, admin_client, artifact_env, change_url, post_upload, retrieve_url,
    stored_path, upload,
)


def _body(response) -> bytes:
    return b''.join(response.streaming_content)


def test_retrieval_by_canonical_id_returns_the_uploaded_bytes(artifact_env):
    artifact = upload(admin_client(), content=PNG_BYTES, filename='design.png')

    response = Client().get(retrieve_url(artifact.id))

    assert response.status_code == 200
    assert _body(response) == PNG_BYTES


def test_retrieval_content_type_is_never_guessed_from_the_file(artifact_env):
    """V4 does not interpret content, so the response asserts nothing about
    what the bytes are."""
    client = admin_client()
    markdown = upload(client, content=MARKDOWN_BYTES, filename='spec.md')
    png = upload(client, content=PNG_BYTES, filename='design.png')

    for artifact in (markdown, png):
        response = Client().get(retrieve_url(artifact.id))
        assert response.headers['Content-Type'] == 'application/octet-stream'
        # Uploaded bytes are served from the same origin as the admin, so
        # a browser that sniffed its way to text/html would have a stored
        # XSS against an admin session. SecurityMiddleware's nosniff
        # (SECURE_CONTENT_TYPE_NOSNIFF, on by default) is what stops it.
        assert response.headers['X-Content-Type-Options'] == 'nosniff'


def test_record_flag_returns_identity_and_nothing_else(artifact_env):
    artifact = upload(admin_client('curator'), content=JAVASCRIPT_BYTES, filename='widget.js')

    response = Client().get(retrieve_url(artifact.id), {'record': 'true'})

    assert response.status_code == 200
    record = json.loads(response.content)
    assert record == {
        'id': str(artifact.id),
        'path': artifact.path,
        'originalFilename': 'widget.js',
        'uploadedBy': 'curator',
        'createdAt': artifact.created_at.isoformat(),
        'updatedAt': artifact.updated_at.isoformat(),
    }


def test_unknown_canonical_id_is_a_404(artifact_env):
    response = Client().get(retrieve_url('3f2c1e5a-7b41-4f0a-9d2e-6c8b0a1d4e77'))

    assert response.status_code == 404


def test_retrieval_is_recorded_in_the_access_log(artifact_env):
    """internal-work-item-service.md REQ-04: a read is recorded in the
    service's own access log, not on Streams."""
    artifact = upload(admin_client(), content=MARKDOWN_BYTES, filename='spec.md')
    assert ArtifactAccessLog.objects.count() == 0

    Client().get(retrieve_url(artifact.id), headers={'X-Actor': 'refinement-agent'})

    entry = ArtifactAccessLog.objects.get()
    assert entry.operation == 'getArtifact'
    assert str(entry.artifact_id) == str(artifact.id)
    assert entry.actor == 'refinement-agent'


def test_a_read_of_an_unknown_id_is_recorded_too(artifact_env):
    Client().get(retrieve_url('3f2c1e5a-7b41-4f0a-9d2e-6c8b0a1d4e77'))

    entry = ArtifactAccessLog.objects.get()
    assert entry.operation == 'getArtifact'
    assert entry.actor == 'http-client'


def test_retrieval_changes_no_state(artifact_env):
    """A read has no state-changing effect: the record and the bytes on the
    volume are untouched by retrieving them."""
    artifact = upload(admin_client(), content=MARKDOWN_BYTES, filename='spec.md')
    before = stored_path(artifact_env, artifact).stat().st_mtime_ns
    updated_at = artifact.updated_at

    for _ in range(3):
        assert Client().get(retrieve_url(artifact.id)).status_code == 200

    artifact.refresh_from_db()
    assert artifact.updated_at == updated_at
    assert stored_path(artifact_env, artifact).stat().st_mtime_ns == before


def test_an_in_place_edit_is_returned_by_the_next_read(artifact_env):
    """REQ-02's acceptance: the file is edited on the volume with no
    upload and no restart, and the next retrieval returns the edited
    bytes. Nothing invalidates a cache, because there is none."""
    artifact = upload(admin_client(), content=MARKDOWN_BYTES, filename='spec.md')

    stored_path(artifact_env, artifact).write_bytes(b'# Edited on the host\n')

    response = Client().get(retrieve_url(artifact.id))
    assert _body(response) == b'# Edited on the host\n'


def test_successive_in_place_edits_are_each_returned(artifact_env):
    """Not a one-off invalidation: every read opens the file again."""
    artifact = upload(admin_client(), content=b'v1', filename='spec.md')
    path = stored_path(artifact_env, artifact)

    for body in (b'v2', b'v3', b'v4 with rather more text than before'):
        path.write_bytes(body)
        assert _body(Client().get(retrieve_url(artifact.id))) == body


def test_an_in_place_edit_does_not_touch_the_record(artifact_env):
    artifact = upload(admin_client(), content=MARKDOWN_BYTES, filename='spec.md')
    stored_path(artifact_env, artifact).write_bytes(b'edited')

    record = json.loads(Client().get(retrieve_url(artifact.id), {'record': 'true'}).content)
    assert record['path'] == artifact.path
    assert record['updatedAt'] == artifact.updated_at.isoformat()


def test_a_record_whose_file_is_gone_is_a_404_not_a_crash(artifact_env):
    """The record says where a copy was put; the filesystem says whether it
    is still there, and the filesystem wins (PRD §8)."""
    artifact = upload(admin_client(), content=MARKDOWN_BYTES, filename='spec.md')
    stored_path(artifact_env, artifact).unlink()

    response = Client().get(retrieve_url(artifact.id))

    assert response.status_code == 404
    assert json.loads(response.content)['path'] == artifact.path
    # The record itself survives; only the bytes are gone.
    assert Client().get(retrieve_url(artifact.id), {'record': 'true'}).status_code == 200


def test_retrieval_refuses_a_write_method(artifact_env):
    """There is no upload endpoint on the retrieval route: upload is the
    admin panel."""
    artifact = upload(admin_client(), content=MARKDOWN_BYTES, filename='spec.md')

    assert Client().post(retrieve_url(artifact.id), data={'x': 'y'}).status_code == 405
    assert Client().put(retrieve_url(artifact.id), data=b'new bytes').status_code == 405


def test_retrieval_returns_the_replaced_bytes_after_a_reupload(artifact_env):
    client = admin_client()
    artifact = upload(client, content=MARKDOWN_BYTES, filename='spec.md')

    assert post_upload(client, content=b'# v2\n', filename='spec.md',
                       url=change_url(artifact.id)).status_code == 302

    assert _body(Client().get(retrieve_url(artifact.id))) == b'# v2\n'
