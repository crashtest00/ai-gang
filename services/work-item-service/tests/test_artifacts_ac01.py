"""
AC-01 (PRD §13) end to end, through the real surfaces:

  "Upload a Markdown file, a PNG and a JavaScript file; retrieve each by
  canonical id and verify the bytes match what was uploaded, with no
  metadata supplied and no git repository involved. Edit one in place on
  the volume and verify the next read returns the edit."

Upload is the Django admin (a logged-in staff user POSTing multipart),
retrieval is a plain GET on the real URL, and the in-place edit is a write
to the file on the volume.
"""

from __future__ import annotations

import json

from django.test import Client

from artifacts.models import Artifact
from tests.artifacts_support import (  # noqa: F401 - committed_artifact_env is a fixture
    JAVASCRIPT_BYTES, MARKDOWN_BYTES, PNG_BYTES, admin_client, committed_artifact_env, files_under, read_events, retrieve_url,
    stored_path, upload,
)

AC01_FILES = [
    ('requirements.md', MARKDOWN_BYTES),
    ('mockup.png', PNG_BYTES),
    ('widget.js', JAVASCRIPT_BYTES),
]


def test_ac01_three_unlike_files_round_trip_and_one_is_edited_in_place(committed_artifact_env):
    client = admin_client('product-owner')

    # Upload: nothing supplied but the files themselves.
    uploaded = {name: upload(client, content=content, filename=name) for name, content in AC01_FILES}
    assert Artifact.objects.count() == 3
    assert len({a.id for a in uploaded.values()}) == 3

    # Retrieve each by canonical id; bytes match exactly.
    for name, content in AC01_FILES:
        artifact = uploaded[name]
        response = Client().get(retrieve_url(artifact.id))
        assert response.status_code == 200, name
        assert b''.join(response.streaming_content) == content, name

        record = json.loads(Client().get(retrieve_url(artifact.id), {'record': 'true'}).content)
        assert record['originalFilename'] == name
        assert record['uploadedBy'] == 'product-owner'

    # Three files on the volume, each at its id-derived path.
    assert files_under(committed_artifact_env.root) == sorted(a.path for a in uploaded.values())

    # Edit one in place on the volume — no upload, no restart.
    edited = uploaded['requirements.md']
    stored_path(committed_artifact_env, edited).write_bytes(b'# Requirements, revised on the host\n')

    response = Client().get(retrieve_url(edited.id))
    assert b''.join(response.streaming_content) == b'# Requirements, revised on the host\n'

    # The other two are untouched, and the edit announced nothing.
    assert b''.join(Client().get(retrieve_url(uploaded['mockup.png'].id)).streaming_content) == PNG_BYTES
    assert len(read_events(committed_artifact_env)) == 3


def test_ac01_needs_no_git_repository(committed_artifact_env):
    """Nothing in the upload or retrieval path touches a repository: the
    whole round trip happens with the artifact root as the only filesystem
    this app writes to."""
    client = admin_client()

    artifact = upload(client, content=MARKDOWN_BYTES, filename='requirements.md')

    assert files_under(committed_artifact_env.root) == [artifact.path]
    assert Client().get(retrieve_url(artifact.id)).status_code == 200
