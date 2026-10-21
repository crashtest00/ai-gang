"""§4 — recovery of Memory's stores: drop and re-migrate Memory's database,
then rebuild the manifest from the archive; cursors issued before the
rebuild are refused (REQ-09, REQ-08's notes read); REQ-03's last
Acceptance case."""

from pathlib import Path

import pytest
from django.core.management import call_command
from django.test import Client

from archive.copier import Copier, digest_of
from archive.models import Note, Session, SessionCommit, SessionFile, SessionPullRequest
from tests.support import Session as Src, bash, completed_run, conv, opening, pr_link, result

pytestmark = pytest.mark.django_db(transaction=True)


def remigrate():
    call_command('migrate', 'archive', 'zero', verbosity=0)
    call_command('migrate', 'archive', verbosity=0)


def test_a_dropped_database_is_rebuilt_from_the_archive_without_duplicating_bytes(roots, clock):
    harness, archive = roots
    copier = Copier(harness_root=harness, archive_root=archive, clock=clock)
    s = Src(harness)
    s.append(opening() + [
        bash('t1', 'git commit -m "x"'), result('t1', '[main 1a2b3c4] x\n'), pr_link(9),
    ] + completed_run()[3:])
    s.write('tool-results/t.txt', b'out\n')
    notes = s.notes_dir()
    (notes / 'MEMORY.md').write_bytes(b'first\n')
    killed = Src(harness)
    killed.append(opening())
    copier.reconcile()
    manifest_before = Client().get(f'/sessions/{s.id}').json()
    session_cursor = Client().get('/sessions').json()['cursor']
    notes_cursor = Client().get('/notes/changes').json()['cursor']

    # Stop the copier; drop and re-migrate; the source moves on meanwhile.
    remigrate()
    assert not Session.objects.exists()
    s.append([conv('system', subtype='stop_hook_summary')])
    (notes / 'MEMORY.md').write_bytes(b'rewritten\n')

    copier = Copier(harness_root=harness, archive_root=archive, clock=clock)
    copier.rebuild_from_archive()
    copier.reconcile()

    for src in sorted(p for p in Path(harness).rglob('*') if p.is_file()):
        assert (Path(archive) / src.relative_to(harness)).read_bytes() == src.read_bytes(), src
    for f in SessionFile.objects.all():
        assert f.digest == digest_of(Path(archive) / f.archive_path)
    assert (Path(archive) / Note.objects.get().archive_path).read_bytes() == b'rewritten\n'

    rebuilt = Session.objects.get(pk=s.id)
    assert rebuilt.project == 'proj' and rebuilt.source_path == manifest_before['source_path']
    assert (rebuilt.status, rebuilt.quiet_by) == ('quiet', 'end_record')
    assert rebuilt.harness_versions == manifest_before['harness_versions']
    assert sorted(f.path for f in rebuilt.files.all()) == sorted(f['path'] for f in manifest_before['files'])
    assert list(SessionPullRequest.objects.values_list('repository', 'number')) == [('org/repo', 9)]
    assert list(SessionCommit.objects.values_list('hash', 'role')) == [('1a2b3c4', 'authored')]
    assert Session.objects.get(pk=killed.id).status == 'active'

    # The counts read's totals restart at zero; record_counts are rebuilt.
    assert Client().get('/counts').json()['versions'] == {}
    assert rebuilt.record_counts['2.1.296']['conversational'] > 0

    # Cursors from before the rebuild are refused by both listings.
    assert Client().get('/sessions', {'since': session_cursor}).status_code == 410
    assert Client().get('/notes/changes', {'since': notes_cursor}).status_code == 410
    assert Client().get('/sessions').status_code == 200
