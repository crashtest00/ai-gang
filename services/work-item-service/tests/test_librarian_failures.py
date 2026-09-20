"""
REQ-06 — every request gets exactly one answer, and every named failure
reason is produced by the case that names it.

"unknown artifact id, unknown destination repository, a ``requested_path``
that does not resolve inside that repository, or a copy that failed" —
plus REQ-01's missing field, which ``test_librarian_contract.py`` covers.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import pytest

from librarian.failures import (
    COPY_FAILED, PATH_OUTSIDE_REPOSITORY, UNKNOWN_ARTIFACT, UNKNOWN_DESTINATION_REPO,
)
from librarian.models import ArtifactDelivery
from librarian.responses import STATUS_FAILED
from librarian.content import SKIPPED_DIRECTORIES
from tests.librarian_support import (  # noqa: F401 - librarian_env is a fixture
    all_responses, await_lock_granted, await_response, files_in, librarian_env, make_repo, publish_request,
    request_delivery, seed_artifact,
)

MOCKUP = b'-a mockup-'


def _failure(response, reason):
    payload = response['payload']
    assert payload['status'] == STATUS_FAILED, payload
    assert payload['reason'] == reason, payload
    assert payload['detail']
    return payload


# --- unknown artifact id -------------------------------------------------

def test_an_id_no_artifact_is_registered_under(librarian_env):
    make_repo(librarian_env, 'hello-web')

    response = request_delivery(librarian_env, artifact_id=uuid.uuid4(), destination_repo='hello-web',
                                requested_path='mockup.png')

    _failure(response, UNKNOWN_ARTIFACT)


def test_an_artifact_id_that_is_not_a_canonical_id(librarian_env):
    make_repo(librarian_env, 'hello-web')

    response = request_delivery(librarian_env, artifact_id='../../etc/passwd', destination_repo='hello-web',
                                requested_path='mockup.png')

    _failure(response, UNKNOWN_ARTIFACT)


# --- unknown destination repository --------------------------------------

def test_a_repository_that_is_not_mounted(librarian_env):
    artifact = seed_artifact(MOCKUP)
    make_repo(librarian_env, 'hello-web')

    response = request_delivery(librarian_env, artifact_id=artifact.id, destination_repo='no-such-project',
                                requested_path='mockup.png')

    _failure(response, UNKNOWN_DESTINATION_REPO)


@pytest.mark.parametrize('name', ['../../etc', 'hello-web/src', '.', '..', '   ', 'a/b'])
def test_a_destination_repo_that_is_not_a_single_directory_name(librarian_env, name):
    artifact = seed_artifact(MOCKUP)
    make_repo(librarian_env, 'hello-web')

    response = request_delivery(librarian_env, artifact_id=artifact.id, destination_repo=name,
                                requested_path='mockup.png')

    assert response['payload']['reason'] in (UNKNOWN_DESTINATION_REPO, 'missing_field')


def test_a_file_under_the_projects_root_is_not_a_repository(librarian_env):
    artifact = seed_artifact(MOCKUP)
    (librarian_env.projects_root / 'notes.txt').write_text('not a repository')

    response = request_delivery(librarian_env, artifact_id=artifact.id, destination_repo='notes.txt',
                                requested_path='mockup.png')

    _failure(response, UNKNOWN_DESTINATION_REPO)


def test_a_project_directory_with_no_working_tree_is_not_a_repository(librarian_env):
    """The repository is `<project>/src`, the git working tree each
    project container sees as /workspace. A project directory without one
    is not somewhere this librarian can deliver, and it is refused rather
    than written beside the project's own compose file."""
    artifact = seed_artifact(MOCKUP)
    project_dir = librarian_env.projects_root / 'hello-web'
    project_dir.mkdir()
    (project_dir / 'docker-compose.yml').write_text('services: {}\n')

    response = request_delivery(librarian_env, artifact_id=artifact.id, destination_repo='hello-web',
                                requested_path='mockup.png')

    payload = _failure(response, UNKNOWN_DESTINATION_REPO)
    assert 'src' in payload['detail']
    assert files_in(project_dir) == ['docker-compose.yml']


def test_a_request_cannot_reach_the_project_directorys_own_compose_file(librarian_env):
    """The project directory holds the project's `docker-compose.yml`,
    `Dockerfile` and `.env`, and Compose merges a
    `docker-compose.override.yml` beside them on the next `up`. None of
    that is repository content: the repository is one level down, so the
    project directory is outside it and `..` is the same refusal as any
    other escape."""
    artifact = seed_artifact(MOCKUP)
    repo = make_repo(librarian_env, 'hello-web')
    project_dir = repo.parent
    (project_dir / 'docker-compose.yml').write_text('services: {}\n')

    response = request_delivery(librarian_env, artifact_id=artifact.id, destination_repo='hello-web',
                                requested_path='../docker-compose.override.yml')

    _failure(response, PATH_OUTSIDE_REPOSITORY)
    assert files_in(project_dir) == ['docker-compose.yml']
    assert files_in(repo) == []


def test_a_symlink_out_of_the_projects_root_is_not_a_repository(librarian_env, tmp_path):
    """Containment is checked on the RESOLVED path, so a symlink planted
    under the projects root cannot borrow a directory outside it."""
    artifact = seed_artifact(MOCKUP)
    outside = tmp_path / 'somewhere-else'
    outside.mkdir()
    (librarian_env.projects_root / 'borrowed').symlink_to(outside)

    response = request_delivery(librarian_env, artifact_id=artifact.id, destination_repo='borrowed',
                                requested_path='mockup.png')

    _failure(response, UNKNOWN_DESTINATION_REPO)
    assert files_in(outside) == []


# --- a requested_path that does not resolve inside that repository -------

@pytest.mark.parametrize('requested_path', [
    '../hello-desktop/stolen.png',
    '../../etc/cron.d/payload',
    '/etc/cron.d/payload',
    'src/../../escaped.png',
    'src/',
    '.',
])
def test_a_requested_path_that_does_not_resolve_inside_the_repository(librarian_env, requested_path):
    artifact = seed_artifact(MOCKUP)
    repo = make_repo(librarian_env, 'hello-web')
    make_repo(librarian_env, 'hello-desktop')

    response = request_delivery(librarian_env, artifact_id=artifact.id, destination_repo='hello-web',
                                requested_path=requested_path)

    _failure(response, PATH_OUTSIDE_REPOSITORY)
    assert files_in(repo) == []
    assert files_in(librarian_env.projects_root) == []


@pytest.mark.parametrize('skipped', sorted(SKIPPED_DIRECTORIES))
def test_a_requested_path_under_a_directory_the_search_skips_is_refused(librarian_env, skipped):
    """The write rule and the search rule are one list. A file delivered
    into `.git` or `node_modules` could never be found again by REQ-03's
    content search, which never descends into either — and `.git` is the
    repository's own object store, which a delivery has no business
    writing into."""
    artifact = seed_artifact(MOCKUP)
    repo = make_repo(librarian_env, 'hello-web')

    response = request_delivery(librarian_env, artifact_id=artifact.id, destination_repo='hello-web',
                                requested_path=f'{skipped}/planted.png')

    payload = _failure(response, PATH_OUTSIDE_REPOSITORY)
    assert skipped in payload['detail']
    assert files_in(repo) == []


def test_a_requested_path_through_a_symlink_that_leaves_the_repository(librarian_env, tmp_path):
    artifact = seed_artifact(MOCKUP)
    repo = make_repo(librarian_env, 'hello-web')
    outside = tmp_path / 'outside-the-repo'
    outside.mkdir()
    (repo / 'link').symlink_to(outside)

    response = request_delivery(librarian_env, artifact_id=artifact.id, destination_repo='hello-web',
                                requested_path='link/planted.png')

    _failure(response, PATH_OUTSIDE_REPOSITORY)
    assert files_in(outside) == []


def test_a_requested_path_containing_a_nul_byte(librarian_env):
    artifact = seed_artifact(MOCKUP)
    make_repo(librarian_env, 'hello-web')

    response = request_delivery(librarian_env, artifact_id=artifact.id, destination_repo='hello-web',
                                requested_path='designs/mockup\x00.png')

    _failure(response, PATH_OUTSIDE_REPOSITORY)


# --- a copy that failed --------------------------------------------------

def test_an_artifact_whose_file_is_gone_from_the_volume(librarian_env):
    """The id resolves; it is the bytes that are missing. That is a copy
    that failed, not an unknown artifact."""
    artifact = seed_artifact(MOCKUP)
    make_repo(librarian_env, 'hello-web')
    Path(artifact.file.path).unlink()

    response = request_delivery(librarian_env, artifact_id=artifact.id, destination_repo='hello-web',
                                requested_path='mockup.png')

    payload = _failure(response, COPY_FAILED)
    assert 'artifact volume' in payload['detail']


def test_a_destination_directory_that_cannot_be_written(librarian_env):
    artifact = seed_artifact(MOCKUP)
    repo = make_repo(librarian_env, 'hello-web')
    locked = repo / 'read-only'
    locked.mkdir()
    locked.chmod(0o500)
    try:
        response = request_delivery(librarian_env, artifact_id=artifact.id, destination_repo='hello-web',
                                    requested_path='read-only/mockup.png')
    finally:
        locked.chmod(0o700)

    _failure(response, COPY_FAILED)
    assert files_in(repo) == []
    assert not ArtifactDelivery.objects.exists()


def test_a_symlink_planted_after_the_check_delivers_nothing_outside_the_repository(librarian_env, settings):
    """The containment check resolves symlinks, but it runs before the
    lock is taken, and both `mkdir(parents=True)` and `os.link` follow
    symlinks in the parent components afterwards. A symlink planted into
    the working tree in between would otherwise have put the artifact in
    another project's repository, with a confirmation naming a path in
    this one.

    The window is entered through the real request path: the librarian
    holds its advisory lock only after the repository and the requested
    path have been resolved and before anything is written, so the moment
    `pg_locks` shows the lock granted is the moment between the check and
    the write.
    """
    settings.LIBRARIAN_LOCK_HOLD_DELAY_MS = 2000
    artifact = seed_artifact(MOCKUP)
    repo = make_repo(librarian_env, 'hello-web')
    victim = make_repo(librarian_env, 'hello-desktop')

    message_id, cursor = publish_request(librarian_env, {
        'requestedBy': 'frontend-agent', 'artifactId': str(artifact.id),
        'destinationRepo': 'hello-web', 'requestedPath': 'designs/mockup.png',
    })
    await_lock_granted(str(artifact.id), 'hello-web')
    (repo / 'designs').symlink_to(victim)

    response = await_response(librarian_env, message_id, cursor)

    payload = _failure(response, COPY_FAILED)
    assert 'containment check' in payload['detail']
    assert files_in(victim) == []
    assert not (victim / 'mockup.png').exists()
    assert not ArtifactDelivery.objects.exists()


# --- "no request completes without a response" ---------------------------

def test_every_request_gets_exactly_one_answer(librarian_env):
    artifact = seed_artifact(MOCKUP)
    make_repo(librarian_env, 'hello-web')
    requests = [
        {'artifact_id': artifact.id, 'destination_repo': 'hello-web', 'requested_path': 'a.png'},
        {'artifact_id': uuid.uuid4(), 'destination_repo': 'hello-web', 'requested_path': 'b.png'},
        {'artifact_id': artifact.id, 'destination_repo': 'nope', 'requested_path': 'c.png'},
        {'artifact_id': artifact.id, 'destination_repo': 'hello-web', 'requested_path': '../d.png'},
        {'artifact_id': artifact.id, 'destination_repo': 'hello-web', 'requested_path': 'e.png'},
    ]

    correlations = {request_delivery(librarian_env, **kwargs)['correlationId'] for kwargs in requests}

    assert len(correlations) == len(requests)
    responses = all_responses(librarian_env)
    assert len(responses) == len(requests)
    assert {r['correlationId'] for r in responses} == correlations
    # And the whole batch leaves the request stream fully acknowledged:
    # no entry pending, nothing dead-lettered. Polled rather than asserted
    # outright because the XACK follows the publish — the answer reaches
    # the requester first, by design.
    from librarian.stream_topology import REQUEST_GROUP, REQUEST_STREAM
    deadline = time.time() + 10
    while time.time() < deadline:
        pending = librarian_env.redis.xpending(REQUEST_STREAM, REQUEST_GROUP)['pending']
        if pending == 0:
            break
        time.sleep(0.05)
    assert pending == 0
    assert librarian_env.redis.xlen(f'{REQUEST_STREAM}:dead') == 0
