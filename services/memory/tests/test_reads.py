"""REQ-08 (manifest, file, notes and counts reads, each logged) and REQ-09
(the changed-since listing), through memory-api's URLs."""

import logging
from pathlib import Path

import pytest
from django.test import Client

from archive.models import Session
from tests.support import Session as Src, completed_run, conv, opening

pytestmark = pytest.mark.django_db


@pytest.fixture
def api():
    return Client()


def test_a_session_is_read_by_id_then_file_by_file_and_every_read_is_logged(roots, copier, api, caplog):
    harness, archive = roots
    s = Src(harness)
    s.append(completed_run())
    s.write('tool-results/big.txt', b'x' * 5000)
    copier.reconcile()
    caplog.set_level(logging.INFO, logger='memory.reads')

    manifest = api.get(f'/sessions/{s.id}').json()
    assert len(manifest['files']) == 2
    for entry in manifest['files']:
        response = api.get(f'/sessions/{s.id}/file', {'path': entry['archive_path']})
        assert response.status_code == 200
        body = b''.join(response.streaming_content)
        assert body == (Path(archive) / entry['archive_path']).read_bytes()
        assert body == (Path(harness) / entry['path']).read_bytes()
    logged = [r.getMessage() for r in caplog.records if r.name == 'memory.reads']
    assert len(logged) == 3
    assert f'/sessions/{s.id}' in logged[0]


def test_a_file_that_is_not_the_sessions_is_refused(roots, copier, api):
    s = Src(roots[0])
    s.append(opening())
    copier.reconcile()
    assert api.get(f'/sessions/{s.id}/file', {'path': '../../etc/passwd'}).status_code == 404
    assert api.get('/sessions/no-such-session').status_code == 404


def test_the_notes_read_lists_reads_and_follows_changes_across_projects(roots, copier, api, caplog):
    harness, archive = roots
    s = Src(harness)
    s.append(opening())
    notes = s.notes_dir()
    (notes / 'MEMORY.md').write_bytes(b'index\n')
    (notes / 'a.md').write_bytes(b'a\n')
    copier.reconcile()
    caplog.set_level(logging.INFO, logger='memory.reads')

    listed = api.get('/notes', {'project': 'proj'}).json()['notes']
    assert [n['path'].rsplit('/', 1)[1] for n in listed] == ['MEMORY.md', 'a.md']
    for note in listed:
        assert {'path', 'archive_path', 'digest', 'read_at', 'removed'} <= set(note)
    body = b''.join(api.get('/notes/file', {'path': listed[1]['archive_path']}).streaming_content)
    assert body == (notes / 'a.md').read_bytes()
    assert len([r for r in caplog.records if r.name == 'memory.reads']) == 2

    cursor = api.get('/notes/changes').json()['cursor']
    other = Src(harness, project='other')
    other.append(opening())
    (other.notes_dir() / 'MEMORY.md').write_bytes(b'other index\n')
    copier.reconcile()
    changed = api.get('/notes/changes', {'since': cursor}).json()
    assert [(n['project'], n['path'].rsplit('/', 1)[1]) for n in changed['notes']] == [('other', 'MEMORY.md')]

    cursor = changed['cursor']
    (notes / 'a.md').write_bytes(b'a, rewritten\n')
    copier.reconcile()
    changed = api.get('/notes/changes', {'since': cursor}).json()
    assert [n['path'].rsplit('/', 1)[1] for n in changed['notes']] == ['a.md']

    cursor = changed['cursor']
    (notes / 'MEMORY.md').unlink()
    copier.reconcile()
    changed = api.get('/notes/changes', {'since': cursor}).json()['notes']
    assert [(n['path'].rsplit('/', 1)[1], n['removed']) for n in changed] == [('MEMORY.md', True)]


def test_the_counts_read_is_logged(roots, api, caplog):
    caplog.set_level(logging.INFO, logger='memory.reads')
    assert api.get('/counts').status_code == 200
    assert any('/counts' in r.getMessage() for r in caplog.records if r.name == 'memory.reads')


def test_sessions_changed_since_a_cursor(roots, copier, clock, api):
    harness = roots[0]
    first, second, third = Src(harness), Src(harness), Src(harness)
    first.append(opening())
    copier.reconcile()
    after_first = api.get('/sessions').json()['cursor']
    second.append(opening())
    third.append(opening())
    copier.reconcile()

    assert set(api.get('/sessions').json()['sessions']) == {first.id, second.id, third.id}
    assert set(api.get('/sessions', {'since': after_first}).json()['sessions']) == {second.id, third.id}

    # Growth: the session appears again after its own earlier position.
    position = api.get('/sessions').json()['cursor']
    first.append([conv('assistant')])
    copier.reconcile()
    assert api.get('/sessions', {'since': position}).json()['sessions'] == [first.id]

    # A status change with no new bytes is a change too.
    position = api.get('/sessions').json()['cursor']
    clock.advance(minutes=36)
    copier.sweep_quiet()
    assert set(api.get('/sessions', {'since': position}).json()['sessions']) == {first.id, second.id, third.id}
    assert Session.objects.filter(status='quiet').count() == 3


def test_a_malformed_cursor_is_refused(api, roots):
    assert api.get('/sessions', {'since': 'nonsense'}).status_code == 400
