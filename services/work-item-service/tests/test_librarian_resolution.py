"""
REQ-02, REQ-03, REQ-04, REQ-05 and AC-06 — the resolution order, the
one-path invariant, the record, and what the confirmation says.

Every branch of REQ-03 is exercised through a published request and the
consumer's own loop; the assertions are against the filesystem, the
``artifact_delivery`` row, and the response stream.
"""

from __future__ import annotations

import pytest

from librarian.delivery import ACTION_ALREADY_PRESENT, ACTION_COPIED, ACTION_RELOCATED
from librarian.models import ArtifactDelivery
from librarian.responses import STATUS_DELIVERED
from tests.librarian_support import (  # noqa: F401 - librarian_env is a fixture
    files_in, librarian_env, make_repo, request_delivery, seed_artifact, start_consumer,
)

MOCKUP = b'\x89PNG\r\n\x1a\n-pretend-this-is-a-mockup-' + b'x' * 512


@pytest.fixture
def delivered(librarian_env):
    """One artifact, one repository, one delivery already made."""
    artifact = seed_artifact(MOCKUP)
    repo = make_repo(librarian_env, 'hello-web', {'README.md': '# hello\n'})
    response = request_delivery(librarian_env, artifact_id=artifact.id, destination_repo='hello-web',
                                requested_path='src/designs/mockup.png', task_id='task-1')
    assert response['payload']['action'] == ACTION_COPIED
    return artifact, repo, response


# --- REQ-03 step 5, first delivery ---------------------------------------

def test_a_first_request_copies_the_bytes_and_answers_with_the_path(librarian_env):
    artifact = seed_artifact(MOCKUP)
    repo = make_repo(librarian_env, 'hello-web')

    response = request_delivery(librarian_env, artifact_id=artifact.id, destination_repo='hello-web',
                                requested_path='src/designs/mockup.png')

    payload = response['payload']
    assert payload['status'] == STATUS_DELIVERED
    assert payload['action'] == ACTION_COPIED
    assert payload['path'] == 'src/designs/mockup.png'
    assert (repo / payload['path']).read_bytes() == MOCKUP
    assert files_in(repo) == ['src/designs/mockup.png']


# --- REQ-03 steps 1-3, the record fast path ------------------------------

def test_a_repeat_request_returns_the_recorded_path_without_a_second_copy(delivered, librarian_env):
    artifact, repo, first = delivered

    second = request_delivery(librarian_env, artifact_id=artifact.id, destination_repo='hello-web',
                              requested_path='src/designs/mockup.png')

    assert second['payload']['action'] == ACTION_ALREADY_PRESENT
    assert second['payload']['path'] == first['payload']['path']
    assert files_in(repo) == ['README.md', 'src/designs/mockup.png']


def test_step_three_is_an_existence_check_not_a_content_comparison(delivered, librarian_env):
    """A delivered file a human then edited is still the delivered file.
    Re-delivering over their edit would be the opposite of REQ-03."""
    artifact, repo, first = delivered
    edited = repo / first['payload']['path']
    edited.write_bytes(b'a human changed this after delivery')

    second = request_delivery(librarian_env, artifact_id=artifact.id, destination_repo='hello-web',
                              requested_path='src/designs/mockup.png')

    assert second['payload']['action'] == ACTION_ALREADY_PRESENT
    assert second['payload']['path'] == first['payload']['path']
    assert edited.read_bytes() == b'a human changed this after delivery'
    assert files_in(repo) == ['README.md', 'src/designs/mockup.png']


# --- REQ-02 --------------------------------------------------------------

def test_a_second_request_naming_a_different_path_gets_the_first_path_back(delivered, librarian_env):
    """REQ-02's acceptance, verbatim: "Two requests for the same artifact
    and repository, naming different ``requested_path`` values, leave
    exactly one copy in that repository, and both responses name the same
    path"."""
    artifact, repo, first = delivered

    second = request_delivery(librarian_env, artifact_id=artifact.id, destination_repo='hello-web',
                              requested_path='completely/elsewhere/other-name.png')

    assert second['payload']['path'] == first['payload']['path'] == 'src/designs/mockup.png'
    assert files_in(repo) == ['README.md', 'src/designs/mockup.png']
    assert not (repo / 'completely').exists()


def test_the_same_artifact_reaches_a_second_repository_independently(delivered, librarian_env):
    """One path per artifact *per repository* — a second repository is a
    second delivery."""
    artifact, first_repo, _ = delivered
    second_repo = make_repo(librarian_env, 'hello-desktop')

    response = request_delivery(librarian_env, artifact_id=artifact.id, destination_repo='hello-desktop',
                                requested_path='assets/mockup.png')

    assert response['payload']['action'] == ACTION_COPIED
    assert files_in(second_repo) == ['assets/mockup.png']
    assert ArtifactDelivery.objects.filter(artifact_id=artifact.id).count() == 2


# --- REQ-03 step 4, the content search -----------------------------------

def test_a_file_moved_within_the_repository_is_found_and_the_record_corrected(delivered, librarian_env):
    artifact, repo, first = delivered
    moved_to = repo / 'src' / 'assets' / 'renamed-by-an-agent.png'
    moved_to.parent.mkdir(parents=True)
    (repo / first['payload']['path']).rename(moved_to)

    second = request_delivery(librarian_env, artifact_id=artifact.id, destination_repo='hello-web',
                              requested_path='src/designs/mockup.png')

    assert second['payload']['action'] == ACTION_RELOCATED
    assert second['payload']['path'] == 'src/assets/renamed-by-an-agent.png'
    assert files_in(repo) == ['README.md', 'src/assets/renamed-by-an-agent.png']
    record = ArtifactDelivery.objects.get(artifact_id=artifact.id, repository='hello-web')
    assert record.path == 'src/assets/renamed-by-an-agent.png'


def test_an_artifact_already_in_the_repository_with_no_record_is_not_copied_again(librarian_env):
    """Step 5's "or the artifact is not in the repository" is a fact about
    the repository, and the content search is what establishes it. Without
    the search on the no-record path, this would be a second copy at a
    second path — exactly what REQ-02 forbids."""
    artifact = seed_artifact(MOCKUP)
    repo = make_repo(librarian_env, 'hello-web', {'vendor/already-here.png': MOCKUP})

    response = request_delivery(librarian_env, artifact_id=artifact.id, destination_repo='hello-web',
                                requested_path='src/designs/mockup.png')

    assert response['payload']['action'] == ACTION_ALREADY_PRESENT
    assert response['payload']['path'] == 'vendor/already-here.png'
    assert files_in(repo) == ['vendor/already-here.png']
    assert ArtifactDelivery.objects.get(artifact_id=artifact.id).path == 'vendor/already-here.png'


def test_the_content_search_does_not_match_a_file_that_only_shares_a_name(librarian_env):
    artifact = seed_artifact(MOCKUP)
    repo = make_repo(librarian_env, 'hello-web', {'src/designs/mockup.png': b'an unrelated image'})

    response = request_delivery(librarian_env, artifact_id=artifact.id, destination_repo='hello-web',
                                requested_path='src/designs/mockup.png')

    assert response['payload']['action'] == ACTION_COPIED
    assert response['payload']['path'] == 'src/designs/mockup-1.png'
    assert (repo / 'src/designs/mockup.png').read_bytes() == b'an unrelated image'
    assert (repo / 'src/designs/mockup-1.png').read_bytes() == MOCKUP


def test_the_content_search_skips_the_git_directory(librarian_env):
    """A delivered file's blob is in ``.git`` byte-for-byte after a commit.
    Answering with a path inside ``.git`` would be worse than useless."""
    artifact = seed_artifact(MOCKUP)
    repo = make_repo(librarian_env, 'hello-web', {'.git/objects/loose/deadbeef': MOCKUP})

    response = request_delivery(librarian_env, artifact_id=artifact.id, destination_repo='hello-web',
                                requested_path='designs/mockup.png')

    assert response['payload']['path'] == 'designs/mockup.png'
    assert response['payload']['action'] == ACTION_COPIED


def test_the_content_search_skips_node_modules(librarian_env):
    artifact = seed_artifact(MOCKUP)
    repo = make_repo(librarian_env, 'hello-web', {'node_modules/some-pkg/asset.png': MOCKUP})

    response = request_delivery(librarian_env, artifact_id=artifact.id, destination_repo='hello-web',
                                requested_path='designs/mockup.png')

    assert response['payload']['action'] == ACTION_COPIED
    assert response['payload']['path'] == 'designs/mockup.png'


def test_the_search_sees_the_artifacts_current_bytes_after_an_in_place_edit(librarian_env):
    """Ingress REQ-02: the file on the volume is the record. An artifact
    edited in place is matched on what it now holds."""
    artifact = seed_artifact(MOCKUP)
    repo = make_repo(librarian_env, 'hello-web', {'vendor/edited.png': b'the new bytes'})
    from pathlib import Path
    Path(artifact.file.path).write_bytes(b'the new bytes')

    response = request_delivery(librarian_env, artifact_id=artifact.id, destination_repo='hello-web',
                                requested_path='designs/mockup.png')

    assert response['payload']['action'] == ACTION_ALREADY_PRESENT
    assert response['payload']['path'] == 'vendor/edited.png'


# --- REQ-05, the record --------------------------------------------------

def test_a_delivery_writes_its_record_synchronously(delivered):
    """"written synchronously as part of handling the request" — the row
    is committed by the time the response the requester is waiting on is
    published, so a requester that reads straight after its answer sees
    it."""
    artifact, _repo, response = delivered

    record = ArtifactDelivery.objects.get(artifact_id=artifact.id, repository='hello-web')
    assert record.path == response['payload']['path']
    assert record.requested_by == 'frontend-agent'
    assert record.task_id == 'task-1'
    assert record.delivered_at is not None


def test_the_record_is_a_postgres_row_that_outlives_the_subscriber(delivered, librarian_env):
    """REQ-05's "survives a restart of the platform": the record is a row
    in the same Postgres instance as canonical work state, not stream
    state. Stop every subscriber, start a new one, and the new process
    answers from the record it never wrote."""
    artifact, _repo, first = delivered
    for consumer in librarian_env.consumers:
        consumer.stop(timeout=5)
    librarian_env.consumers.clear()
    start_consumer(librarian_env, 'librarian-test-restarted')

    response = request_delivery(librarian_env, artifact_id=artifact.id, destination_repo='hello-web',
                                requested_path='somewhere/else.png')

    assert response['payload']['action'] == ACTION_ALREADY_PRESENT
    assert response['payload']['path'] == first['payload']['path']


def test_one_row_per_artifact_and_repository_is_a_database_constraint(delivered):
    """REQ-02 at the schema level. The constraint is
    ``uniq_artifact_delivery_pair`` in
    ``librarian/migrations/0001_initial.py``; this asserts Postgres itself
    refuses the second row, not that the application avoids writing one."""
    from django.db import IntegrityError, transaction
    artifact, _repo, _response = delivered

    with pytest.raises(IntegrityError), transaction.atomic():
        ArtifactDelivery.objects.create(
            artifact_id=artifact.id, repository='hello-web', path='a/second/path.png',
            requested_by='someone', task_id=None,
        )


# --- AC-06 ---------------------------------------------------------------

def test_ac06_end_to_end(librarian_env):
    """PRD §13 AC-06, in one run: "Request delivery of an artifact into a
    repository, verify the file is present at the path the confirmation
    named, then repeat the request and verify the same path comes back
    with no second copy. Delete the delivered file, request again, and
    verify it is restored"."""
    artifact = seed_artifact(MOCKUP, filename='design-export.png')
    repo = make_repo(librarian_env, 'hello-web', {'README.md': '# hello\n'})

    first = request_delivery(librarian_env, artifact_id=artifact.id, destination_repo='hello-web',
                             requested_path='src/designs/design-export.png')
    delivered_path = repo / first['payload']['path']
    assert first['payload']['action'] == ACTION_COPIED
    assert delivered_path.read_bytes() == MOCKUP

    second = request_delivery(librarian_env, artifact_id=artifact.id, destination_repo='hello-web',
                              requested_path='src/designs/design-export.png')
    assert second['payload']['path'] == first['payload']['path']
    assert second['payload']['action'] == ACTION_ALREADY_PRESENT
    assert files_in(repo) == ['README.md', 'src/designs/design-export.png']

    delivered_path.unlink()

    third = request_delivery(librarian_env, artifact_id=artifact.id, destination_repo='hello-web',
                             requested_path='src/designs/design-export.png')
    assert third['payload']['action'] == ACTION_COPIED
    assert third['payload']['path'] == first['payload']['path']
    assert delivered_path.read_bytes() == MOCKUP
    assert files_in(repo) == ['README.md', 'src/designs/design-export.png']
    assert ArtifactDelivery.objects.filter(artifact_id=artifact.id).count() == 1
