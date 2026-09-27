"""
REQ-07's second half — "MUST create the destination file atomically so
that correctness does not depend on the delivery record having been
written first."

The lock (``test_librarian_concurrency.py``) serializes two librarians
against each other. It cannot serialize the librarian against an agent
writing in the same repository, which is what the atomic create is for.
Both of its branches are exercised here, through a published request and
the real consumer loop — ``os.link`` is replaced at the OS level so the
run takes the branch, not so the test can call it directly.
"""

from __future__ import annotations

import errno
import os

from librarian.delivery import ACTION_COPIED
from tests.librarian_support import (  # noqa: F401 - librarian_env is a fixture
    files_in, librarian_env, make_repo, request_delivery, seed_artifact,
)

MOCKUP = b'-a design export-'


def test_a_racing_writer_that_takes_the_name_first_does_not_lose_its_file(librarian_env, monkeypatch):
    """``os.link`` refuses rather than overwrites, and the librarian steps
    to the next name. Without that, an unrelated file created between the
    free-name check and the write would be silently destroyed."""
    artifact = seed_artifact(MOCKUP)
    repo = make_repo(librarian_env, 'hello-web')
    real_link = os.link
    raced = {'done': False}

    def racing_link(source, destination, **kwargs):
        if not raced['done']:
            raced['done'] = True
            # Someone else creates the file the librarian was about to.
            with open(destination, 'wb') as handle:
                handle.write(b'written by an agent a microsecond earlier')
            raise FileExistsError(errno.EEXIST, 'File exists', str(destination))
        return real_link(source, destination, **kwargs)

    monkeypatch.setattr(os, 'link', racing_link)

    response = request_delivery(librarian_env, artifact_id=artifact.id, destination_repo='hello-web',
                                requested_path='designs/mockup.png')

    assert raced['done']
    assert response['payload']['action'] == ACTION_COPIED
    assert response['payload']['path'] == 'designs/mockup-1.png'
    assert (repo / 'designs/mockup.png').read_bytes() == b'written by an agent a microsecond earlier'
    assert (repo / 'designs/mockup-1.png').read_bytes() == MOCKUP


def test_delivery_works_on_a_filesystem_that_cannot_hard_link(librarian_env, monkeypatch):
    """The fallback: claim the name with ``O_CREAT|O_EXCL``, which refuses
    to overwrite for the same reason, then rename over the placeholder."""
    artifact = seed_artifact(MOCKUP)
    repo = make_repo(librarian_env, 'hello-web', {'designs/mockup.png': b'an unrelated file'})

    def no_hard_links(source, destination, **kwargs):
        raise OSError(errno.EPERM, 'hard links are not supported here')

    monkeypatch.setattr(os, 'link', no_hard_links)

    response = request_delivery(librarian_env, artifact_id=artifact.id, destination_repo='hello-web',
                                requested_path='designs/mockup.png')

    assert response['payload']['action'] == ACTION_COPIED
    assert response['payload']['path'] == 'designs/mockup-1.png'
    assert (repo / 'designs/mockup-1.png').read_bytes() == MOCKUP
    assert (repo / 'designs/mockup.png').read_bytes() == b'an unrelated file'
    assert files_in(repo) == ['designs/mockup-1.png', 'designs/mockup.png']
