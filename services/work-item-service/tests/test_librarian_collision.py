"""
REQ-04 — "the confirmation reports what happened, not what was asked".

Its acceptance, verbatim: "Deliver to a ``requested_path`` the librarian
must adjust — a name collision with an unrelated file — and the response
names the adjusted path, matching where the file is on disk."
"""

from __future__ import annotations

import pytest

from librarian.delivery import ACTION_COPIED
from librarian.models import ArtifactDelivery
from tests.librarian_support import (  # noqa: F401 - librarian_env is a fixture
    files_in, librarian_env, make_repo, request_delivery, seed_artifact,
)

MOCKUP = b'-the artifact bytes-'
UNRELATED = b'-something else entirely-'


def test_a_name_collision_is_adjusted_and_the_response_names_the_adjusted_path(librarian_env):
    artifact = seed_artifact(MOCKUP)
    repo = make_repo(librarian_env, 'hello-web', {'designs/mockup.png': UNRELATED})

    response = request_delivery(librarian_env, artifact_id=artifact.id, destination_repo='hello-web',
                                requested_path='designs/mockup.png')

    payload = response['payload']
    assert payload['action'] == ACTION_COPIED
    assert payload['path'] == 'designs/mockup-1.png'
    assert (repo / payload['path']).read_bytes() == MOCKUP
    # The unrelated file is untouched: an atomic create never clobbers.
    assert (repo / 'designs/mockup.png').read_bytes() == UNRELATED
    assert ArtifactDelivery.objects.get(artifact_id=artifact.id).path == 'designs/mockup-1.png'


def test_the_response_never_echoes_the_requested_path(librarian_env):
    """The point of REQ-04: "A confirmation that repeated the request would
    report success for a copy that silently landed somewhere else, or
    nowhere"."""
    artifact = seed_artifact(MOCKUP)
    repo = make_repo(librarian_env, 'hello-web', {
        'designs/mockup.png': UNRELATED,
        'designs/mockup-1.png': UNRELATED + b'!',
    })

    response = request_delivery(librarian_env, artifact_id=artifact.id, destination_repo='hello-web',
                                requested_path='designs/mockup.png')

    assert response['payload']['path'] == 'designs/mockup-2.png'
    assert response['payload']['path'] != 'designs/mockup.png'
    assert (repo / response['payload']['path']).read_bytes() == MOCKUP
    assert files_in(repo) == ['designs/mockup-1.png', 'designs/mockup-2.png', 'designs/mockup.png']


@pytest.mark.parametrize('requested,expected', [
    ('designs/mockup.png', 'designs/mockup-1.png'),
    ('NOTES', 'NOTES-1'),
    ('.gitignore', '.gitignore-1'),
    ('archive.tar.gz', 'archive.tar-1.gz'),
])
def test_the_adjustment_rule(librarian_env, requested, expected):
    """The rule, stated once in ``delivery._collision_candidates``: ``-1``,
    ``-2``, ... before the extension, where the extension is the last dot
    in the name and a leading dot is not one."""
    artifact = seed_artifact(MOCKUP)
    repo = make_repo(librarian_env, 'hello-web', {requested: UNRELATED})

    response = request_delivery(librarian_env, artifact_id=artifact.id, destination_repo='hello-web',
                                requested_path=requested)

    assert response['payload']['path'] == expected
    assert (repo / expected).read_bytes() == MOCKUP


def test_a_requested_path_occupied_by_a_directory_is_adjusted(librarian_env):
    artifact = seed_artifact(MOCKUP)
    repo = make_repo(librarian_env, 'hello-web', {'designs/assets/keep.txt': b'x'})

    response = request_delivery(librarian_env, artifact_id=artifact.id, destination_repo='hello-web',
                                requested_path='designs/assets')

    assert response['payload']['path'] == 'designs/assets-1'
    assert (repo / 'designs/assets').is_dir()
    assert (repo / 'designs/assets-1').read_bytes() == MOCKUP


def test_the_adjustment_is_deterministic(librarian_env):
    """The same repository state and the same requested path give the same
    answer, in two different repositories."""
    artifact = seed_artifact(MOCKUP)
    for name in ('hello-web', 'hello-desktop'):
        make_repo(librarian_env, name, {'designs/mockup.png': UNRELATED})

    first = request_delivery(librarian_env, artifact_id=artifact.id, destination_repo='hello-web',
                             requested_path='designs/mockup.png')
    second = request_delivery(librarian_env, artifact_id=artifact.id, destination_repo='hello-desktop',
                              requested_path='designs/mockup.png')

    assert first['payload']['path'] == second['payload']['path'] == 'designs/mockup-1.png'
