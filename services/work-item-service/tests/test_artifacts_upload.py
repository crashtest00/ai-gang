"""
REQ-01 — upload creates the artifact and its identity.

Every test here drives the REAL upload surface: a logged-in staff user
POSTing multipart to the Django admin's add view. Nothing calls the model
or the storage layer directly to stand in for an upload.
"""

from __future__ import annotations

from artifacts.models import Artifact
from tests.artifacts_support import (  # noqa: F401 - artifact_env is a fixture
    ADD_URL, JAVASCRIPT_BYTES, MARKDOWN_BYTES, PNG_BYTES, admin_client, artifact_env, post_upload, stored_path, upload,
)


def test_upload_registers_id_path_filename_actor_and_time(artifact_env):
    client = admin_client('uploader')

    artifact = upload(client, content=MARKDOWN_BYTES, filename='prd.md')

    assert artifact.id is not None
    assert artifact.path == f'{str(artifact.id)[:2]}/{artifact.id}'
    assert artifact.original_filename == 'prd.md'
    assert artifact.uploaded_by == 'uploader'
    assert artifact.created_at is not None
    assert artifact.updated_at is not None
    assert stored_path(artifact_env, artifact).read_bytes() == MARKDOWN_BYTES


def test_upload_needs_nothing_but_the_file(artifact_env):
    """REQ-01: "no metadata supplied alongside it". The add form is posted
    with the file part and an empty original_filename, which is what a
    browser sends when the uploader fills nothing in."""
    client = admin_client()

    response = post_upload(client, content=PNG_BYTES, filename='mockup.png')

    assert response.status_code == 302
    artifact = Artifact.objects.get()
    assert artifact.original_filename == 'mockup.png'


def test_upload_accepts_unlike_file_types_and_never_inspects_them(artifact_env):
    """No extension, MIME or content validation of any kind: a PNG, a
    JavaScript file, a Markdown file, an extensionless file, a file whose
    extension lies about its bytes, and an empty file are all accepted."""
    client = admin_client()

    cases = [
        (MARKDOWN_BYTES, 'spec.md'),
        (PNG_BYTES, 'design.png'),
        (JAVASCRIPT_BYTES, 'widget.js'),
        (b'\x00\x01\x02\xff\xfe', 'no-extension-at-all'),
        (PNG_BYTES, 'actually-a-png.txt'),
        (b'', 'empty'),
        (b'x' * 200_000, 'large-ish.bin'),
    ]

    for content, filename in cases:
        artifact = upload(client, content=content, filename=filename)
        assert stored_path(artifact_env, artifact).read_bytes() == content, filename

    assert Artifact.objects.count() == len(cases)


def test_no_record_field_is_derived_from_content(artifact_env):
    """Two uploads of the same bytes under different names differ only in
    the fields identity supplies — there is no type, status or
    content-derived column to disagree about (PRD §8)."""
    client = admin_client()

    first = upload(client, content=PNG_BYTES, filename='a.png')
    second = upload(client, content=PNG_BYTES, filename='b.bin')

    assert first.id != second.id
    assert first.path != second.path
    assert {f.name for f in Artifact._meta.get_fields()} == {
        'id', 'file', 'original_filename', 'uploaded_by', 'created_at', 'updated_at',
    }


def test_every_upload_gets_its_own_canonical_id_and_path(artifact_env):
    client = admin_client()

    artifacts = [upload(client, content=MARKDOWN_BYTES, filename='same-name.md') for _ in range(3)]

    assert len({a.id for a in artifacts}) == 3
    assert len({a.path for a in artifacts}) == 3


def test_anonymous_upload_is_refused(artifact_env):
    """The upload surface is authenticated: there is no unauthenticated
    HTTP upload endpoint, and the admin's own login wall is what enforces
    it."""
    from django.test import Client

    response = post_upload(Client(), content=MARKDOWN_BYTES, filename='sneaked-in.md')

    assert response.status_code in (302, 403)
    assert Artifact.objects.count() == 0


def test_uploading_actor_is_the_logged_in_user_not_a_supplied_header(artifact_env):
    """The recorded actor is the authenticated admin user. An `X-Actor`
    header — the self-declared convention workitems' unauthenticated HTTP
    write endpoints accept — has no effect here, which is the point of
    making the admin the only upload surface."""
    from tests.artifacts_support import encode_multipart_verbatim

    client = admin_client('real-human')
    payload, content_type = encode_multipart_verbatim(
        {'original_filename': ''}, file_field='file', filename='note.md', content=MARKDOWN_BYTES,
    )

    response = client.post(ADD_URL, data=payload, content_type=content_type,
                           headers={'X-Actor': 'someone-else'})

    assert response.status_code == 302
    assert Artifact.objects.get().uploaded_by == 'real-human'


def test_the_admin_upload_surface_renders(artifact_env):
    """The add form, the changelist and the change form of a stored
    artifact all render — a ModelAdmin can be misconfigured in ways only a
    GET of these pages catches."""
    client = admin_client()
    artifact = upload(client, content=MARKDOWN_BYTES, filename='spec.md')

    for url in (
        '/django-admin/artifacts/artifact/',
        ADD_URL,
        f'/django-admin/artifacts/artifact/{artifact.id}/change/',
        '/django-admin/artifacts/artifactaccesslog/',
        '/django-admin/',
    ):
        assert client.get(url).status_code == 200, url


def test_an_artifact_cannot_be_deleted_from_the_admin(artifact_env):
    """V4 specifies no deletion, and Django's default would delete the
    record while leaving the file on the volume — the record/filesystem
    disagreement PRD §8 rules out."""
    client = admin_client()
    artifact = upload(client, content=MARKDOWN_BYTES, filename='spec.md')

    response = client.post(f'/django-admin/artifacts/artifact/{artifact.id}/delete/')

    assert response.status_code == 403
    assert Artifact.objects.filter(id=artifact.id).exists()
    assert stored_path(artifact_env, artifact).exists()
