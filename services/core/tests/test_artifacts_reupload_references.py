"""
REQ-04's second half — "every reference already made to it" survives a
re-upload.

The work-item side's own artifact-reference field is `feat/v4-work-item-
references`' REQ, not this track's. What this track CAN prove, without
touching `workitems/`, is that a reference held elsewhere by canonical id
still resolves to the same artifact after a re-upload — because the
re-upload changes neither the id nor the path. V2's existing
`work_item_artifact` association table stands in as the holder of that
reference.
"""

from __future__ import annotations

import uuid

from django.test import Client

from artifacts.models import Artifact
from tests.artifacts_support import (  # noqa: F401 - artifact_env is a fixture
    MARKDOWN_BYTES, admin_client, artifact_env, change_url, post_upload, retrieve_url, stored_path, upload,
)
from workitems.models import WorkItem, WorkItemArtifact

PROJECT = 'artifact-reference-test'


def _work_item_referencing(artifact) -> WorkItemArtifact:
    item = WorkItem.objects.create(
        id=uuid.uuid4(), project=PROJECT, type='task', display_name='Build from the spec', status='proposed',
    )
    return WorkItemArtifact.objects.create(
        work_item=item, artifact_type='artifact', reference=str(artifact.id),
    )


def test_a_reference_by_canonical_id_still_resolves_after_a_reupload(artifact_env):
    client = admin_client()
    artifact = upload(client, content=MARKDOWN_BYTES, filename='spec.md')
    reference = _work_item_referencing(artifact)

    assert post_upload(client, content=b'# Superseded body\n', filename='spec-v2.md',
                       url=change_url(artifact.id)).status_code == 302

    reference.refresh_from_db()
    resolved = Artifact.objects.get(id=uuid.UUID(reference.reference))
    assert resolved.id == artifact.id
    assert resolved.path == artifact.path
    assert stored_path(artifact_env, resolved).read_bytes() == b'# Superseded body\n'

    response = Client().get(retrieve_url(reference.reference))
    assert response.status_code == 200
    assert b''.join(response.streaming_content) == b'# Superseded body\n'
