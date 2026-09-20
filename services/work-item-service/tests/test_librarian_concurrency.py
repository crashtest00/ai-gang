"""
REQ-07 — "Two requests for the same artifact and repository arriving
together MUST produce one copy and one path."

Its acceptance: "Two requests published simultaneously for the same
artifact and repository result in one file and two responses naming the
same path."

**Genuinely concurrent, and proved so.** Two real subscriber threads, each
with its own Redis connection and its own database connection, both inside
a blocking ``XREADGROUP`` before either request is published — so Redis
hands one entry to each. While they run, this test watches Postgres's own
``pg_locks`` for the advisory lock keyed on the (artifact, repository)
pair and asserts it is held by one session and *waited on* by another.
That is the serialization REQ-07 requires, observed in the database rather
than inferred from the outcome.

``settings.LIBRARIAN_LOCK_HOLD_DELAY_MS`` makes the window wide enough to
observe. It is zero in every deployment — the same test-only-knob shape as
``RELAY_ROW_DELAY_MS`` (``workitems/relay.py``).
"""

from __future__ import annotations

import time

from librarian.delivery import ACTION_ALREADY_PRESENT, ACTION_COPIED, advisory_lock_key
from librarian.models import ArtifactDelivery
from librarian.responses import STATUS_DELIVERED
from tests.librarian_support import (  # noqa: F401 - librarian_env is a fixture
    advisory_lock_holders, await_response, files_in, librarian_env, make_repo, publish_request, seed_artifact,
    start_consumer,
)

MOCKUP = b'-a design export-' * 4096
LOCK_HOLD_MS = 1500


def _two_subscribers(env):
    """Replace the fixture's single subscriber with two, both blocking
    long enough that neither is between reads when the requests land."""
    for consumer in env.consumers:
        consumer.stop(timeout=5)
    env.consumers.clear()
    start_consumer(env, 'librarian-a', block_ms=30000)
    start_consumer(env, 'librarian-b', block_ms=30000)


def _await_contention(artifact_id: str, repository: str, timeout: float = 10) -> tuple[int, int]:
    deadline = time.time() + timeout
    best = (0, 0)
    while time.time() < deadline:
        granted, waiting = advisory_lock_holders(artifact_id, repository)
        if granted and waiting:
            return granted, waiting
        best = max(best, (granted, waiting))
        time.sleep(0.02)
    return best


def test_two_simultaneous_requests_produce_one_file_and_one_path(librarian_env, settings):
    settings.LIBRARIAN_LOCK_HOLD_DELAY_MS = LOCK_HOLD_MS
    artifact = seed_artifact(MOCKUP, filename='design-export.png')
    repo = make_repo(librarian_env, 'hello-web', {'README.md': '# hello\n'})
    _two_subscribers(librarian_env)

    first_id, first_cursor = publish_request(librarian_env, {
        'requestedBy': 'frontend-agent', 'artifactId': str(artifact.id),
        'destinationRepo': 'hello-web', 'requestedPath': 'designs/mockup.png',
    })
    second_id, second_cursor = publish_request(librarian_env, {
        'requestedBy': 'backend-agent', 'artifactId': str(artifact.id),
        'destinationRepo': 'hello-web', 'requestedPath': 'designs/mockup.png',
    })

    granted, waiting = _await_contention(str(artifact.id), 'hello-web')
    assert (granted, waiting) == (1, 1), (
        f'expected one session holding the pair lock and one waiting on it, saw granted={granted} waiting={waiting}'
    )

    first = await_response(librarian_env, first_id, first_cursor, timeout=30)
    second = await_response(librarian_env, second_id, second_cursor, timeout=30)

    assert first['payload']['status'] == second['payload']['status'] == STATUS_DELIVERED
    assert first['payload']['path'] == second['payload']['path'] == 'designs/mockup.png'
    assert sorted([first['payload']['action'], second['payload']['action']]) == \
        sorted([ACTION_ALREADY_PRESENT, ACTION_COPIED])
    assert files_in(repo) == ['README.md', 'designs/mockup.png']
    assert (repo / 'designs/mockup.png').read_bytes() == MOCKUP
    assert ArtifactDelivery.objects.filter(artifact_id=artifact.id, repository='hello-web').count() == 1


def test_two_simultaneous_requests_naming_different_paths_still_produce_one_file(librarian_env, settings):
    """REQ-02 and REQ-07 together: the loser of the race is answered with
    the winner's path, not with its own."""
    settings.LIBRARIAN_LOCK_HOLD_DELAY_MS = LOCK_HOLD_MS
    artifact = seed_artifact(MOCKUP, filename='design-export.png')
    repo = make_repo(librarian_env, 'hello-web')
    _two_subscribers(librarian_env)

    first_id, first_cursor = publish_request(librarian_env, {
        'requestedBy': 'frontend-agent', 'artifactId': str(artifact.id),
        'destinationRepo': 'hello-web', 'requestedPath': 'a/first.png',
    })
    second_id, second_cursor = publish_request(librarian_env, {
        'requestedBy': 'backend-agent', 'artifactId': str(artifact.id),
        'destinationRepo': 'hello-web', 'requestedPath': 'b/second.png',
    })

    assert _await_contention(str(artifact.id), 'hello-web') == (1, 1)

    first = await_response(librarian_env, first_id, first_cursor, timeout=30)
    second = await_response(librarian_env, second_id, second_cursor, timeout=30)

    assert first['payload']['path'] == second['payload']['path']
    assert first['payload']['path'] in ('a/first.png', 'b/second.png')
    assert files_in(repo) == [first['payload']['path']]


def test_different_pairs_do_not_serialize_against_each_other(librarian_env):
    """The lock is keyed on the pair, so two deliveries that share
    nothing take different keys."""
    first = advisory_lock_key('3f2c1e5a-7b41-4f0a-9d2e-6c8b0a1d4e77', 'hello-web')
    assert first == advisory_lock_key('3f2c1e5a-7b41-4f0a-9d2e-6c8b0a1d4e77', 'hello-web')
    assert first != advisory_lock_key('3f2c1e5a-7b41-4f0a-9d2e-6c8b0a1d4e77', 'hello-desktop')
    assert first != advisory_lock_key('00000000-0000-0000-0000-000000000000', 'hello-web')
