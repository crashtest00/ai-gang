"""
Shared helpers for the artifact-ingress tests
(strategy/v4.0/features/artifact-ingress.md).

Not collected by pytest (pytest.ini's `python_files = test_*.py`); imported
by the `test_artifacts_*.py` modules. It exists so those modules can share
one fixture and one raw-multipart POST helper without touching the
service-wide conftest.py, which this track does not own.

**Why raw multipart instead of `SimpleUploadedFile`.** Django's own test
client runs `os.path.basename(file.name)` when it encodes a file part, so
a traversal-shaped filename never reaches the server at all and REQ-05's
acceptance case could not be exercised through the real upload path. These
helpers build the multipart body by hand, so the `filename=` parameter on
the wire is exactly what a test asks for.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from artifacts.events import ARTIFACT_EVENT_STREAM

ADD_URL = '/django-admin/artifacts/artifact/add/'

# AC-01's three unlike files. The PNG is a real 1x1 image; the point of the
# criterion is that the platform treats all three identically.
MARKDOWN_BYTES = b'# A Specification\n\nBody text, with UTF-8: \xc3\xa9\xe2\x80\x94.\n'
PNG_BYTES = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=='
)
JAVASCRIPT_BYTES = b"export function render(x) { return `<p>${x}</p>`; }\n"


def change_url(artifact_id) -> str:
    return f'/django-admin/artifacts/artifact/{artifact_id}/change/'


def retrieve_url(artifact_id) -> str:
    return f'/artifacts/{artifact_id}'


def _build_env(settings, tmp_path, redis_client):
    root = tmp_path / 'artifact-root'
    root.mkdir()
    settings.ARTIFACT_ROOT = str(root)
    # Every test here creates an admin user and logs in, and Django's
    # default PBKDF2 hasher costs about a second per test doing it. The
    # upload path does not care how the password was hashed.
    settings.PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']
    return SimpleNamespace(root=root, redis=redis_client)


@pytest.fixture
def artifact_env(db, settings, tmp_path, redis_client):
    """Real Postgres and real Redis, with an artifact root on a per-test
    temp directory.

    Uses pytest-django's rolled-back `db` fixture, so nothing here commits
    and `transaction.on_commit` never fires — which means an upload made
    under this fixture publishes NO event. Tests that observe the event
    stream must use `committed_artifact_env` instead. The split is for
    speed: `transactional_db` truncates every table and re-creates content
    types and permissions after each test, and at ~50 tests that dominates
    the suite's runtime.
    """
    return _build_env(settings, tmp_path, redis_client)


@pytest.fixture
def committed_artifact_env(transactional_db, settings, tmp_path, redis_client):
    """As `artifact_env`, but every write is a real commit, so the
    `transaction.on_commit` publication in ArtifactAdmin.save_model
    actually runs and the real Redis stream can be read back."""
    return _build_env(settings, tmp_path, redis_client)


def admin_client(username: str = 'artifact-admin') -> Client:
    User = get_user_model()
    User.objects.create_superuser(username, f'{username}@example.com', 'password123')
    client = Client()
    assert client.login(username=username, password='password123')
    return client


def encode_multipart_verbatim(fields: dict, *, file_field: str, filename: str, content: bytes,
                              boundary: str = 'ArtifactIngressBoundary') -> tuple[bytes, str]:
    """A multipart body whose file part carries `filename` byte-for-byte."""
    parts = []
    for key, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
        )
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
        f'Content-Type: application/octet-stream\r\n\r\n'.encode()
        + content
        + b'\r\n'
    )
    parts.append(f'--{boundary}--\r\n'.encode())
    return b''.join(parts), f'multipart/form-data; boundary={boundary}'


def post_upload(client: Client, *, content: bytes, filename: str, original_filename: str = '',
                url: str = ADD_URL):
    """Upload through the REAL admin add/change view: a logged-in staff
    user POSTing multipart, exactly as a browser does."""
    body, content_type = encode_multipart_verbatim(
        {'original_filename': original_filename}, file_field='file', filename=filename, content=content,
    )
    return client.post(url, data=body, content_type=content_type)


def upload(client: Client, *, content: bytes, filename: str, original_filename: str = ''):
    """Upload and assert the admin accepted it, returning the new record."""
    from artifacts.models import Artifact

    before = set(Artifact.objects.values_list('id', flat=True))
    response = post_upload(client, content=content, filename=filename, original_filename=original_filename)
    assert response.status_code == 302, _form_errors(response)
    created = set(Artifact.objects.values_list('id', flat=True)) - before
    assert len(created) == 1, f'expected exactly one new artifact, got {created}'
    return Artifact.objects.get(id=created.pop())


def _form_errors(response) -> str:
    context = getattr(response, 'context', None)
    if not context:
        return f'status {response.status_code}'
    try:
        return f'status {response.status_code}: {context["adminform"].form.errors.as_text()}'
    except Exception:  # noqa: BLE001 - diagnostics only
        return f'status {response.status_code}'


def stored_path(env, artifact) -> Path:
    """The absolute path of an artifact's file on the volume, derived the
    way a human on the host would derive it: root, two-character shard,
    full id."""
    return Path(env.root) / str(artifact.id)[:2] / str(artifact.id)


def read_events(env) -> list:
    """Every envelope currently on the real artifact event stream."""
    entries = env.redis.xrange(ARTIFACT_EVENT_STREAM)
    return [json.loads(fields['data']) for _entry_id, fields in entries]


def files_under(root: Path) -> list:
    return sorted(
        os.path.relpath(os.path.join(dirpath, name), root)
        for dirpath, _dirnames, filenames in os.walk(root)
        for name in filenames
    )
