"""
REQ-02 (documented, id-derived layout), REQ-04 (re-upload replaces content
under the same id and path) and REQ-05 (values that become paths are
validated).

The traversal cases go through the real upload path: a logged-in admin
POSTing a multipart body whose `filename=` parameter is
`../../etc/passwd` byte-for-byte on the wire.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from django.conf import settings
from django.core.exceptions import SuspiciousFileOperation

from artifacts.models import Artifact
from artifacts.storage import artifact_relative_path
from tests.artifacts_support import (  # noqa: F401 - artifact_env is a fixture
    MARKDOWN_BYTES, PNG_BYTES, admin_client, artifact_env, change_url, files_under, post_upload, retrieve_url,
    stored_path, upload,
)

TRAVERSAL = '../../etc/passwd'


# --- Layout (REQ-02, open question §6.1) --------------------------------

def test_layout_is_two_character_shard_then_full_id(artifact_env):
    client = admin_client()

    artifact = upload(client, content=MARKDOWN_BYTES, filename='spec.md')

    identifier = str(artifact.id)
    assert artifact.path == f'{identifier[:2]}/{identifier}'
    assert files_under(artifact_env.root) == [f'{identifier[:2]}/{identifier}']


def test_stored_file_carries_no_extension_from_the_original_name(artifact_env):
    client = admin_client()

    artifact = upload(client, content=PNG_BYTES, filename='mockup.png')

    assert Path(artifact.path).suffix == ''
    assert 'mockup' not in artifact.path


def test_recorded_path_and_storage_agree(artifact_env):
    """One column holds the path, and it is the same string the storage
    layer resolves — there is no second place for them to disagree."""
    client = admin_client()

    artifact = upload(client, content=MARKDOWN_BYTES, filename='spec.md')
    fresh = Artifact.objects.get(id=artifact.id)

    assert fresh.path == fresh.file.name
    assert Path(fresh.file.path) == stored_path(artifact_env, fresh)


# --- REQ-05: validation of values that become paths ---------------------

def test_traversal_filename_is_stored_verbatim_and_never_reaches_the_path(artifact_env):
    """REQ-05's acceptance, through the real upload path: the file is
    stored inside the artifact root, is retrievable by canonical id, and
    the recorded original filename is the untrusted string verbatim."""
    client = admin_client()

    artifact = upload(client, content=MARKDOWN_BYTES, filename=TRAVERSAL, original_filename=TRAVERSAL)

    assert artifact.original_filename == TRAVERSAL
    assert artifact.path == f'{str(artifact.id)[:2]}/{artifact.id}'

    resolved = Path(artifact.file.path).resolve()
    root = Path(settings.ARTIFACT_ROOT).resolve()
    assert root in resolved.parents
    assert resolved.read_bytes() == MARKDOWN_BYTES
    assert files_under(artifact_env.root) == [artifact.path]

    response = client.get(retrieve_url(artifact.id))
    assert response.status_code == 200
    assert b''.join(response.streaming_content) == MARKDOWN_BYTES


def test_nothing_is_written_outside_the_artifact_root(artifact_env, tmp_path):
    """The sibling directory a `../../` name aims at stays empty."""
    outside = tmp_path / 'etc'
    outside.mkdir()
    client = admin_client()

    upload(client, content=MARKDOWN_BYTES, filename=TRAVERSAL, original_filename=TRAVERSAL)

    assert list(outside.iterdir()) == []


def test_multipart_filename_header_alone_is_not_the_path(artifact_env):
    """With no original_filename supplied, the name comes from the wire.
    Django's own MultiPartParser sanitizes it to `passwd` before any
    application code sees it, so `original_filename` is NOT verbatim in
    this case — the honest limit of the platform's "opaque string". The
    path is unaffected either way."""
    client = admin_client()

    artifact = upload(client, content=MARKDOWN_BYTES, filename=TRAVERSAL)

    assert artifact.original_filename == 'passwd'
    assert artifact.path == f'{str(artifact.id)[:2]}/{artifact.id}'
    assert files_under(artifact_env.root) == [artifact.path]


@pytest.mark.parametrize('bad_identifier', [
    '../../etc/passwd', '/etc/passwd', 'not-a-uuid', '', '..', 'a/b',
    '3f2c1e5a-7b41-4f0a-9d2e-6c8b0a1d4e77/../../etc/passwd', None,
])
def test_only_a_canonical_id_can_become_a_path_component(bad_identifier):
    with pytest.raises(SuspiciousFileOperation):
        artifact_relative_path(bad_identifier)


def test_a_canonical_id_is_re_derived_not_echoed():
    """An id in a different case or surrounded by URN/brace noise still
    yields the canonical form, because the path is built from the parsed
    UUID rather than from the input string."""
    canonical = '3f2c1e5a-7b41-4f0a-9d2e-6c8b0a1d4e77'

    for variant in (canonical.upper(), f'urn:uuid:{canonical}', '{' + canonical + '}', canonical.replace('-', '')):
        assert artifact_relative_path(variant) == f'3f/{canonical}'


# --- REQ-04: re-upload overwrites in place ------------------------------

def test_reupload_keeps_the_id_and_the_path_and_replaces_the_bytes(artifact_env):
    client = admin_client()
    artifact = upload(client, content=MARKDOWN_BYTES, filename='spec.md')
    original_id, original_path = artifact.id, artifact.path

    response = post_upload(client, content=b'# Rewritten\n', filename='spec-v2.md', url=change_url(original_id))

    assert response.status_code == 302
    replaced = Artifact.objects.get(id=original_id)
    assert replaced.path == original_path
    assert stored_path(artifact_env, replaced).read_bytes() == b'# Rewritten\n'
    # One file on the volume, not two: Django's default collision rename
    # is off (artifacts/storage.py).
    assert files_under(artifact_env.root) == [original_path]
    assert Artifact.objects.count() == 1


def test_reupload_of_a_spooled_large_upload_also_overwrites_in_place(artifact_env, settings):
    """An upload above FILE_UPLOAD_MAX_MEMORY_SIZE is spooled to a temp
    file and moved rather than streamed, a path that would otherwise spin
    forever against an existing destination — see ArtifactFileStorage._save.
    Forcing the threshold to 0 exercises it with a small file."""
    settings.FILE_UPLOAD_MAX_MEMORY_SIZE = 0
    client = admin_client()

    artifact = upload(client, content=b'first spooled body', filename='big.bin')
    response = post_upload(client, content=b'second spooled body', filename='big.bin',
                           url=change_url(artifact.id))

    assert response.status_code == 302
    replaced = Artifact.objects.get(id=artifact.id)
    assert replaced.path == artifact.path
    assert stored_path(artifact_env, replaced).read_bytes() == b'second spooled body'
    assert files_under(artifact_env.root) == [artifact.path]


def test_editing_only_the_original_filename_leaves_the_bytes_alone(artifact_env):
    client = admin_client()
    artifact = upload(client, content=MARKDOWN_BYTES, filename='spec.md')

    response = client.post(change_url(artifact.id), data={'original_filename': 'renamed.md'})

    assert response.status_code == 302
    edited = Artifact.objects.get(id=artifact.id)
    assert edited.original_filename == 'renamed.md'
    assert edited.path == artifact.path
    assert stored_path(artifact_env, edited).read_bytes() == MARKDOWN_BYTES


def test_artifact_root_is_the_configured_setting(artifact_env):
    """The root comes from settings.ARTIFACT_ROOT at call time, not from a
    value cached when the storage object was built — otherwise pointing a
    deployment at a different volume would silently do nothing."""
    client = admin_client()

    artifact = upload(client, content=MARKDOWN_BYTES, filename='spec.md')

    assert os.path.commonpath([artifact.file.path, settings.ARTIFACT_ROOT]) == \
        os.path.abspath(settings.ARTIFACT_ROOT)
