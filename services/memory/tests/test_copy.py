"""REQ-03 — every session is copied into the archive, verbatim and
append-only; notes at their latest version; rewrites flagged; files the
harness deletes keep their copies. Driven through Copier.sync_path and
Copier.reconcile, the routine both of memory-copier's triggers call."""

from pathlib import Path

import pytest

from archive.copier import digest_of
from archive.models import Note, Session, SessionFile
from tests.support import (CWD, PROJECT, Session as Src, archive_bytes, completed_run, conv, line, opening)

pytestmark = pytest.mark.django_db


def every_file(root):
    return sorted(p for p in Path(root).rglob('*') if p.is_file())


def test_a_session_with_a_subagent_and_an_oversized_result_is_copied_byte_for_byte(roots, copier):
    harness, archive = roots
    s = Src(harness)
    s.append(opening())
    copier.reconcile()
    assert archive_bytes(archive, harness, s.transcript) == s.transcript.read_bytes()

    # It grows: a subagent appears partway through, with its .meta.json
    # (no trailing newline) and an oversized tool result.
    agent = s.sidecar('subagents', 'agent-a1.jsonl')
    s.append([conv('user'), conv('assistant')], path=agent)
    s.write('subagents/agent-a1.meta.json', b'{"agentType":"general-purpose"}')
    s.write('tool-results/toolu_1.txt', b'1\n2\n' * 100000)
    s.append([conv('assistant')])
    for path in (agent, s.sidecar('subagents', 'agent-a1.meta.json'), s.sidecar('tool-results', 'toolu_1.txt'),
                 s.transcript):
        copier.sync_path(s.parts(path))
    session = Session.objects.get(pk=s.id)
    assert session.status == 'active'
    meta = SessionFile.objects.get(path='/'.join(s.parts(s.sidecar('subagents', 'agent-a1.meta.json'))))
    assert meta.bytes_read == len(b'{"agentType":"general-purpose"}')
    assert meta.partial_tail is False

    s.append(completed_run()[3:])
    copier.reconcile()
    for src in every_file(harness):
        assert archive_bytes(archive, harness, src) == src.read_bytes(), src
    assert not any(f.partial_tail for f in SessionFile.objects.exclude(path__endswith='.jsonl'))
    assert Session.objects.get(pk=s.id).source_path == f'{PROJECT}/{CWD}/{s.id}.jsonl'


def test_a_record_still_being_written_is_never_copied_in_part(roots, copier):
    harness, archive = roots
    s = Src(harness)
    s.append(opening())
    whole = line(conv('assistant'))
    s.append([whole[:40]])
    copier.sync_path(s.parts(s.transcript))
    copied = archive_bytes(archive, harness, s.transcript)
    assert copied.endswith(b'\n')
    assert copied == s.transcript.read_bytes()[: -40]
    s.append([whole[40:]])
    copier.sync_path(s.parts(s.transcript))
    assert archive_bytes(archive, harness, s.transcript) == s.transcript.read_bytes()
    assert SessionFile.objects.get(path__endswith='.jsonl').partial_tail is False


def test_a_stopped_copier_copies_the_whole_session_when_it_starts(roots, copier):
    harness, archive = roots
    s = Src(harness)
    s.append(completed_run())
    s.write('tool-results/t.txt', b'out\n')
    copier.reconcile()
    for src in every_file(harness):
        assert archive_bytes(archive, harness, src) == src.read_bytes()


def test_a_truncated_source_marks_the_session_rewritten_and_keeps_the_copy(roots, copier):
    harness, archive = roots
    s = Src(harness)
    s.append(completed_run())
    copier.reconcile()
    before = archive_bytes(archive, harness, s.transcript)
    s.transcript.write_bytes(before[:50])
    copier.reconcile()
    assert Session.objects.get(pk=s.id).status == 'rewritten'
    assert archive_bytes(archive, harness, s.transcript) == before


def test_a_changed_prefix_marks_the_session_rewritten_even_when_the_file_grew(roots, copier):
    harness, archive = roots
    s = Src(harness)
    s.append(opening())
    copier.reconcile()
    before = archive_bytes(archive, harness, s.transcript)
    s.transcript.write_bytes(b'X' + before[1:] + line(conv('assistant')))
    copier.sync_path(s.parts(s.transcript))
    assert Session.objects.get(pk=s.id).status == 'rewritten'
    assert archive_bytes(archive, harness, s.transcript) == before
    # A rewritten session stays rewritten (REQ-06: rewritten overrides).
    s.append([conv('assistant'), {'type': 'cost-state'}])
    copier.reconcile()
    assert Session.objects.get(pk=s.id).status == 'rewritten'


def test_a_killed_runs_trailing_bytes_are_copied_only_once_the_session_is_quiet(roots, copier, clock):
    harness, archive = roots
    s = Src(harness)
    s.append(opening() + [conv('assistant')])  # killed: no end record
    copier.reconcile()
    clock.advance(minutes=36)
    copier.sweep_quiet()
    assert Session.objects.get(pk=s.id).status == 'quiet'

    copied_before = archive_bytes(archive, harness, s.transcript)
    s.append([b'{"type":"assistant","partial'])
    copier.reconcile()
    session = Session.objects.get(pk=s.id)
    assert session.status == 'active'
    assert archive_bytes(archive, harness, s.transcript) == copied_before

    clock.advance(minutes=20)
    copier.sweep_quiet()
    assert Session.objects.get(pk=s.id).status == 'active'
    clock.advance(minutes=16)
    copier.sweep_quiet()
    assert Session.objects.get(pk=s.id).status == 'quiet'
    assert archive_bytes(archive, harness, s.transcript) == s.transcript.read_bytes()
    transcript = SessionFile.objects.get(path='/'.join(s.parts(s.transcript)))
    assert transcript.partial_tail is True

    # A later record joined to the fragment is one unparseable line, and the
    # archive stays byte-identical (REQ-03, REQ-05).
    s.append(completed_run())
    copier.reconcile()
    assert archive_bytes(archive, harness, s.transcript) == s.transcript.read_bytes()
    counts = Session.objects.get(pk=s.id).record_counts
    assert counts['2.1.296']['unparseable'] == 1


def test_notes_keep_their_latest_version_and_temporary_files_are_never_archived(roots, copier):
    harness, archive = roots
    s = Src(harness)
    s.append(completed_run())
    notes = s.notes_dir()
    (notes / 'MEMORY.md').write_bytes(b'- [Test command](test-command.md)\n')
    (notes / 'test-command.md').write_bytes(b'npm test\n')
    copier.reconcile()

    # The agent's tools write a temporary file and rename it over the note.
    for tmp_name in ('MEMORY.mdXyZ123', 'note.md.tmp.1.abc'):
        (notes / tmp_name).write_bytes(b'temporary\n')
        copier.sync_path(s.parts(notes / tmp_name))
        copier.reconcile()
        assert not Note.objects.filter(path__endswith=tmp_name).exists()
        assert not (Path(archive) / PROJECT / CWD / 'memory' / tmp_name).exists()
    (notes / 'note.md.tmp.1.abc').write_bytes(b'npm run test:all\n')
    (notes / 'note.md.tmp.1.abc').rename(notes / 'test-command.md')
    (notes / 'MEMORY.mdXyZ123').unlink()
    copier.sync_path(s.parts(notes / 'test-command.md'))
    copier.reconcile()

    note = Note.objects.get(path__endswith='test-command.md')
    assert (Path(archive) / note.archive_path).read_bytes() == b'npm run test:all\n'
    assert note.digest == digest_of(notes / 'test-command.md')
    assert sorted(n.path.rsplit('/', 1)[1] for n in Note.objects.all()) == ['MEMORY.md', 'test-command.md']
    assert Session.objects.get(pk=s.id).status != 'rewritten'

    (notes / 'test-command.md').unlink()
    copier.reconcile()
    note = Note.objects.get(path__endswith='test-command.md')
    assert note.removed is True
    assert (Path(archive) / note.archive_path).read_bytes() == b'npm run test:all\n'


def test_a_quiet_session_deleted_at_the_source_keeps_its_copies_and_manifest(roots, copier):
    harness, archive = roots
    s = Src(harness)
    s.append(completed_run())
    s.write('tool-results/t.txt', b'out\n')
    copier.reconcile()
    session = Session.objects.get(pk=s.id)
    assert session.status == 'quiet'
    files_before = list(SessionFile.objects.values_list('path', 'bytes_read', 'digest'))
    archived = {p: p.read_bytes() for p in every_file(archive)}

    s.transcript.unlink()
    for p in sorted(s.sidecar().rglob('*'), reverse=True):
        p.unlink() if p.is_file() else p.rmdir()
    s.sidecar().rmdir()
    copier.reconcile()

    after = Session.objects.get(pk=s.id)
    assert after.status == 'quiet' and after.change_seq == session.change_seq
    assert list(SessionFile.objects.values_list('path', 'bytes_read', 'digest')) == files_before
    assert {p: p.read_bytes() for p in every_file(archive)} == archived


def test_the_offset_is_the_archive_copys_length_when_the_manifest_disagrees(roots, copier):
    harness, archive = roots
    s = Src(harness)
    s.append(opening())
    copier.reconcile()
    # A copier stopped after writing the archive but before recording it.
    rec = SessionFile.objects.get(path__endswith='.jsonl')
    s.append([conv('assistant')])
    arch = Path(archive) / rec.archive_path
    arch.write_bytes(s.transcript.read_bytes())
    SessionFile.objects.filter(pk=rec.pk).update(bytes_read=3)
    s.append([conv('assistant')])
    copier.reconcile()
    assert arch.read_bytes() == s.transcript.read_bytes()
    rec.refresh_from_db()
    assert rec.bytes_read == len(s.transcript.read_bytes())
    assert rec.digest == digest_of(arch)


def test_an_unchanged_source_still_corrects_a_manifest_offset_to_the_archive_length(roots, copier):
    harness, archive = roots
    s = Src(harness)
    s.append(completed_run())
    copier.reconcile()
    rec = SessionFile.objects.get(path__endswith='.jsonl')
    SessionFile.objects.filter(pk=rec.pk).update(bytes_read=7, digest='sha256:stale')
    copier.reconcile()
    rec.refresh_from_db()
    arch = Path(archive) / rec.archive_path
    assert rec.bytes_read == arch.stat().st_size
    assert rec.digest == digest_of(arch)
    assert arch.read_bytes() == s.transcript.read_bytes()
