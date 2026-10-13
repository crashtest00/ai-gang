"""The copy routine (Durable Agent Session Record REQ-03 … REQ-07, §4).

It is kept apart from what triggers it (REQ-03): `Copier.sync_path` does the
same thing whether a file-change event (`watcher.py`) or the reconciling
pass (`Copier.reconcile`) asked for it — read past the stored offset,
compare the copied prefix, append what the copy rule allows. The event only
makes the copy prompt; the pass makes it correct.

Paths are handled as tuples of components relative to the harness volume's
root (`project, encoded_cwd, ...`). The archive keeps each file under the
same relative path below its own root, so a session's `project` and
`source_path` can be read back from the archive alone (§4, rebuild).

Every statement about what a harness file or record *is* is asked of
`harness.py`; nothing here parses a record's fields.
"""

import hashlib
import logging
import os
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from . import harness
from .models import (
    QUIET_BY_END_RECORD, QUIET_BY_INTERVAL, ROLE_AUTHORED, ROLE_MERGED,
    STATUS_ACTIVE, STATUS_QUIET, STATUS_REWRITTEN,
    HarnessVersionTotals, Note, Session, SessionCommit, SessionFile,
    SessionPullRequest, next_change_seq,
)

log = logging.getLogger('memory.copier')

CHUNK = 1 << 20
EMPTY_DIGEST = 'sha256:' + hashlib.sha256(b'').hexdigest()


def _digest_file(path, limit=None):
    h = hashlib.sha256()
    remaining = limit
    with open(path, 'rb') as fh:
        while remaining is None or remaining > 0:
            chunk = fh.read(CHUNK if remaining is None else min(CHUNK, remaining))
            if not chunk:
                break
            h.update(chunk)
            if remaining is not None:
                remaining -= len(chunk)
    return h


def digest_of(path):
    return 'sha256:' + _digest_file(path).hexdigest()


def _ends_partial(path, size):
    """Whether a file of `size` bytes ends in bytes after its last newline."""
    if size == 0:
        return False
    with open(path, 'rb') as fh:
        fh.seek(size - 1)
        return fh.read(1) != b'\n'


def _add_total(version, field):
    totals, _ = HarnessVersionTotals.objects.get_or_create(version=version)
    HarnessVersionTotals.objects.filter(pk=totals.pk).update(**{field: F(field) + 1})


class _SessionChange:
    """What one sync did to a session, so it is saved — and its change
    sequence advanced (REQ-09: new bytes, a new file, a change of status) —
    once."""

    def __init__(self):
        self.changed = False
        self.went_quiet = False


class Copier:
    def __init__(self, harness_root=None, archive_root=None, clock=None, quiet_interval=None):
        self.harness_root = Path(harness_root or settings.MEMORY_HARNESS_ROOT)
        self.archive_root = Path(archive_root or settings.MEMORY_ARCHIVE_ROOT)
        self.clock = clock or timezone.now
        self.quiet_interval = quiet_interval or timedelta(seconds=settings.MEMORY_QUIET_INTERVAL_SECONDS)

    # ------------------------------------------------------------------
    # Entry points: one path (an event, or one file of the pass), the
    # reconciling pass, the interval sweep, and the rebuild.

    def sync_path(self, parts):
        """Bring one path's archive copy up to date. Returns nothing; a path
        Memory does not copy is ignored."""
        parts = tuple(parts)
        kind = harness.classify_path(parts)
        if kind is None:
            return
        try:
            if kind[0] == harness.KIND_NOTE:
                self._sync_note(parts)
            else:
                self._sync_session_file(parts, kind[1])
        except OSError as exc:
            log.warning('could not copy %s: %s', '/'.join(parts), exc)

    def sync_tree(self, parts=()):
        """Sync every file at or below a path on the harness volume (a new
        directory an event reported, or the whole volume)."""
        top = self.harness_root.joinpath(*parts)
        if parts and not top.is_dir():
            # A file, or a path that is gone (a note removed or renamed away).
            self.sync_path(parts)
            return
        for dirpath, dirnames, filenames in os.walk(top):
            dirnames.sort()
            rel = Path(dirpath).relative_to(self.harness_root).parts
            for name in sorted(filenames):
                full = Path(dirpath, name)
                if full.is_symlink() or not full.is_file():
                    continue
                self.sync_path(rel + (name,))

    def reconcile(self):
        """The reconciling pass (REQ-03): every file on the volume, notes the
        source no longer has, then the quiet-interval sweep (REQ-06)."""
        started = self.clock()
        if self.harness_root.is_dir():
            self.sync_tree()
        for note in Note.objects.filter(removed=False).only('path'):
            parts = tuple(note.path.split('/'))
            if not self.harness_root.joinpath(*parts).is_file():
                self.sync_path(parts)
        self.sweep_quiet()
        log.info('reconciling pass finished (started %s)', started.isoformat())

    def sweep_quiet(self):
        """Mark `quiet` every `active` session none of whose files has grown
        for the quiet interval (REQ-06). Each candidate's files are synced
        first, so growth an event missed is seen before the session is
        called quiet."""
        cutoff = self.clock() - self.quiet_interval
        for session_id in list(Session.objects.filter(status=STATUS_ACTIVE, last_growth__lt=cutoff)
                               .values_list('session_id', flat=True)):
            for path in list(SessionFile.objects.filter(session_id=session_id).values_list('path', flat=True)):
                self.sync_path(tuple(path.split('/')))
            with transaction.atomic():
                session = Session.objects.select_for_update().get(pk=session_id)
                if session.status != STATUS_ACTIVE or session.last_growth >= cutoff:
                    continue
                session.status = STATUS_QUIET
                session.quiet_by = QUIET_BY_INTERVAL
                _add_total(session.last_record_version, 'quiet_interval')
                session.change_seq = next_change_seq()
                session.save()
                log.info('session %s quiet by interval', session_id)
            self._copy_tails(session_id)

    def rebuild_from_archive(self):
        """§4: after a lost database, rebuild every session and note the
        archive holds but the manifest does not, by re-reading the archive —
        never by offset. Counts read totals are not added to, and no
        transition is counted."""
        if not self.archive_root.is_dir():
            return 0
        known = set(Session.objects.values_list('session_id', flat=True))
        by_session = {}
        notes = []
        for dirpath, dirnames, filenames in os.walk(self.archive_root):
            dirnames.sort()
            rel = Path(dirpath).relative_to(self.archive_root).parts
            for name in sorted(filenames):
                parts = rel + (name,)
                kind = harness.classify_path(parts)
                if kind is None:
                    continue
                if kind[0] == harness.KIND_NOTE:
                    notes.append(parts)
                elif kind[1] not in known:
                    by_session.setdefault(kind[1], []).append(parts)
        now = self.clock()
        for session_id, files in sorted(by_session.items()):
            # The transcript first, then its sidecar, so run state comes
            # from the transcript in order.
            files.sort(key=lambda p: (not harness.is_session_transcript(p), p))
            with transaction.atomic():
                first = files[0]
                session = Session.objects.create(
                    session_id=session_id, project=first[0],
                    source_path='/'.join(harness.transcript_parts(first[0], first[1], session_id)),
                    first_seen=now, last_growth=now, change_seq=next_change_seq())
                for parts in files:
                    arch = self.archive_root.joinpath(*parts)
                    size = arch.stat().st_size
                    frec = SessionFile.objects.create(
                        session=session, path='/'.join(parts), archive_path='/'.join(parts),
                        bytes_read=size, digest=digest_of(arch), seen_size=size,
                        partial_tail=harness.is_line_file(parts) and _ends_partial(arch, size))
                    if harness.is_classified_file(parts):
                        self._parse(session, frec, parts, _SessionChange(), rebuild=True)
                        frec.save()
                session.status = STATUS_QUIET if session.run_ended else STATUS_ACTIVE
                session.quiet_by = QUIET_BY_END_RECORD if session.run_ended else None
                session.save()
        known_notes = set(Note.objects.values_list('path', flat=True))
        for parts in notes:
            path = '/'.join(parts)
            if path in known_notes:
                continue
            Note.objects.create(project=parts[0], path=path, archive_path=path,
                                digest=digest_of(self.archive_root.joinpath(*parts)),
                                read_at=now, change_seq=next_change_seq())
        if by_session or notes:
            log.info('rebuilt %d session(s) and checked %d note(s) from the archive', len(by_session), len(notes))
        return len(by_session)

    # ------------------------------------------------------------------
    # Session files.

    def _sync_session_file(self, parts, session_id):
        src = self.harness_root.joinpath(*parts)
        try:
            st = src.stat()
        except FileNotFoundError:
            # The harness deletes old sessions (REQ-03): the archive copy,
            # the manifest entry and the status all stay as they are.
            return
        if not src.is_file():
            return
        path = '/'.join(parts)
        arch = self.archive_root.joinpath(*parts)
        now = self.clock()
        change = _SessionChange()
        with transaction.atomic():
            session = Session.objects.select_for_update().filter(pk=session_id).first()
            if session is None:
                session = Session(
                    session_id=session_id, project=parts[0],
                    source_path='/'.join(harness.transcript_parts(parts[0], parts[1], session_id)),
                    first_seen=now, last_growth=now, status=STATUS_ACTIVE)
                change.changed = True
            frec = SessionFile.objects.filter(path=path).first()
            arch_len = arch.stat().st_size if arch.exists() else 0
            if frec is None:
                if session._state.adding:
                    session.change_seq = next_change_seq()
                    session.save()
                frec = SessionFile(session=session, path=path, archive_path=path, bytes_read=arch_len,
                                   digest=digest_of(arch) if arch_len else EMPTY_DIGEST, seen_size=arch_len)
                change.changed = True
            if frec.frozen:
                return
            # §4: the offset is the archive copy's length. A manifest that
            # disagrees (a copier stopped between writing the archive and
            # recording it) takes the length, so no byte is copied twice.
            if frec.bytes_read != arch_len:
                frec.bytes_read = arch_len
                frec.digest = digest_of(arch) if arch_len else EMPTY_DIGEST
            line_file = harness.is_line_file(parts)
            unchanged = (frec.pk is not None and st.st_size == frec.seen_size
                         and st.st_mtime_ns == frec.seen_mtime_ns)
            # Nothing to do when the source is as last seen and everything
            # the copy rule allows is already copied: all of it, or, for a
            # `.jsonl` file of an active session, its complete lines.
            pending_tail = line_file and session.status == STATUS_QUIET and st.st_size > frec.bytes_read
            if unchanged and (st.st_size == frec.bytes_read or (line_file and not pending_tail)):
                # Keep the offset taken from the archive's length (§4).
                frec.save(update_fields=['bytes_read', 'digest'])
                return
            if st.st_size > frec.seen_size:
                # Growth (REQ-06): a session whose newest run has not ended
                # returns to active; one whose run has ended stays quiet
                # unless what it grew by holds a prompt record (_apply_run).
                session.last_growth = now
                if session.status == STATUS_QUIET and not session.run_ended:
                    session.status = STATUS_ACTIVE
                    change.changed = True
            frec.seen_size = max(frec.seen_size, st.st_size)
            frec.seen_mtime_ns = st.st_mtime_ns

            with open(src, 'rb') as fh:
                if not self._prefix_matches(fh, frec, st.st_size):
                    self._mark_rewritten(session, frec, change)
                else:
                    appended = self._append(fh, arch, frec, st.st_size, line_file, whole=not line_file)
                    if appended:
                        change.changed = True
                    if harness.is_classified_file(parts):
                        self._parse(session, frec, parts, change)
                    if line_file and session.status == STATUS_QUIET and not frec.frozen:
                        if self._append(fh, arch, frec, st.st_size, line_file, whole=True):
                            change.changed = True
            frec.save()
            if change.changed:
                session.change_seq = next_change_seq()
            session.save()
        if change.went_quiet:
            self._copy_tails(session_id)

    def _prefix_matches(self, fh, frec, size):
        """REQ-03: a source that shrank, or whose already-copied prefix no
        longer hashes to the archive copy's digest, has been rewritten."""
        if size < frec.bytes_read:
            return False
        if frec.bytes_read == 0:
            return True
        h = hashlib.sha256()
        fh.seek(0)
        remaining = frec.bytes_read
        while remaining > 0:
            chunk = fh.read(min(CHUNK, remaining))
            if not chunk:
                return False
            h.update(chunk)
            remaining -= len(chunk)
        return 'sha256:' + h.hexdigest() == frec.digest

    def _mark_rewritten(self, session, frec, change):
        frec.frozen = True
        if session.status != STATUS_REWRITTEN:
            session.status = STATUS_REWRITTEN
            change.changed = True
        log.warning('session %s: %s was rewritten at the source; the archive copy is kept as it was',
                    session.session_id, frec.path)

    def _append(self, fh, arch, frec, size, line_file, whole):
        """Append source bytes past the offset to the archive copy: up to
        the last newline for a `.jsonl` file of an active session, every
        byte otherwise (REQ-03). Returns the number of bytes appended."""
        start = frec.bytes_read
        if size <= start:
            return 0
        end = size
        if not whole:
            end = self._last_newline_end(fh, start, size)
            if end <= start:
                return 0
        arch.parent.mkdir(parents=True, exist_ok=True)
        # The prefix check hashed exactly the copied bytes, so the digest
        # of the new copy continues from the archive's own bytes.
        h = _digest_file(arch) if arch.exists() else hashlib.sha256()
        fh.seek(start)
        remaining = end - start
        with open(arch, 'ab') as out:
            while remaining > 0:
                chunk = fh.read(min(CHUNK, remaining))
                if not chunk:
                    break
                out.write(chunk)
                h.update(chunk)
                remaining -= len(chunk)
            out.flush()
            os.fsync(out.fileno())
        written = (end - start) - remaining
        frec.bytes_read = start + written
        frec.digest = 'sha256:' + h.hexdigest()
        if line_file:
            frec.partial_tail = _ends_partial(arch, frec.bytes_read)
        return written

    @staticmethod
    def _last_newline_end(fh, start, size):
        pos = size
        while pos > start:
            step = min(CHUNK, pos - start)
            fh.seek(pos - step)
            chunk = fh.read(step)
            idx = chunk.rfind(b'\n')
            if idx >= 0:
                return pos - step + idx + 1
            pos -= step
        return start

    def _copy_tails(self, session_id):
        """Once a session is quiet, copy every `.jsonl` file's bytes after
        its last newline as they are, marking `partial_tail` (REQ-03)."""
        for path in list(SessionFile.objects.filter(session_id=session_id, frozen=False)
                         .values_list('path', flat=True)):
            parts = tuple(path.split('/'))
            if harness.is_line_file(parts):
                self.sync_path(parts)

    # ------------------------------------------------------------------
    # REQ-05 classification, REQ-06 run state, REQ-07 tracing.

    def _parse(self, session, frec, parts, change, rebuild=False):
        """Read the archive copy's complete lines past `parsed_to` and apply
        each. A quiet partial tail is classified once its line completes, so
        a later joined line is one (unparseable) line (REQ-03, REQ-05)."""
        arch = self.archive_root.joinpath(*parts)
        if not arch.exists():
            return
        with open(arch, 'rb') as fh:
            fh.seek(frec.parsed_to)
            data = fh.read(max(0, frec.bytes_read - frec.parsed_to))
        end = data.rfind(b'\n')
        if end < 0:
            return
        state = frec.parse_state or {}
        state.setdefault('last_version', None)
        state.setdefault('pos', 0)
        state.setdefault('squash_pos', -1)
        state.setdefault('last_hash_pos', -1)
        state.setdefault('pending', {})
        is_transcript = harness.is_session_transcript(parts)
        versions = list(session.harness_versions or [])
        counts = session.record_counts or {}
        for line in data[:end].split(b'\n'):
            c = harness.classify_line(line)
            if c.version is not None:
                version = c.version
                state['last_version'] = version
                if version not in versions:
                    versions.append(version)
            else:
                version = state['last_version'] or harness.UNKNOWN_VERSION
            per = counts.setdefault(version, {k: 0 for k in harness.CLASSES})
            per[c.cls] = per.get(c.cls, 0) + 1
            if not rebuild and c.cls in (harness.UNKNOWN, harness.UNPARSEABLE):
                _add_total(version, c.cls)
            if c.record is None:
                if is_transcript:
                    session.last_record_version = version
                continue
            if is_transcript:
                session.last_record_version = version
                self._apply_run(session, c.record, version, change, rebuild)
            self._apply_trace(session, c.record, state)
        frec.parsed_to += end + 1
        frec.parse_state = state
        session.harness_versions = versions
        session.record_counts = counts

    def _apply_run(self, session, record, version, change, rebuild):
        if harness.is_prompt_record(record):
            session.run_ended = False
            if not rebuild and session.status == STATUS_QUIET:
                session.status = STATUS_ACTIVE
                change.changed = True
        elif harness.is_end_record(record):
            session.run_ended = True
            if not rebuild and session.status == STATUS_ACTIVE:
                session.status = STATUS_QUIET
                session.quiet_by = QUIET_BY_END_RECORD
                _add_total(version, 'quiet_end_record')
                change.changed = True
                change.went_quiet = True

    def _apply_trace(self, session, record, state):
        pr = harness.pull_request(record)
        if pr is not None:
            repository, number, url, ts = pr
            SessionPullRequest.objects.get_or_create(
                session_id=session.session_id, repository=repository, number=number,
                defaults={'url': url, 'first_seen': ts})
            return
        for tool_use_id, _name, command in harness.tool_uses(record):
            state['pos'] += 1
            if harness.is_squash_command(command):
                state['squash_pos'] = state['pos']
            if command is not None:
                # The most recent squash up to and including this command.
                state['pending'][tool_use_id] = [harness.is_git_command(command), state['squash_pos']]
        results = harness.tool_results(record)
        if not results:
            return
        ts = harness.record_timestamp(record)
        for tool_use_id, text in results:
            pending = state['pending'].pop(tool_use_id, None)
            if pending is None or not pending[0]:
                continue
            squash_pos = pending[1]
            for commit_hash in harness.commit_hashes(text):
                state['pos'] += 1
                merged = squash_pos >= 0 and squash_pos > state['last_hash_pos']
                state['last_hash_pos'] = state['pos']
                SessionCommit.objects.get_or_create(
                    session_id=session.session_id, hash=commit_hash,
                    defaults={'role': ROLE_MERGED if merged else ROLE_AUTHORED, 'time': ts})

    # ------------------------------------------------------------------
    # Notes (REQ-03's note rule).

    def _sync_note(self, parts):
        path = '/'.join(parts)
        src = self.harness_root.joinpath(*parts)
        arch = self.archive_root.joinpath(*parts)
        now = self.clock()
        with transaction.atomic():
            note = Note.objects.select_for_update().filter(path=path).first()
            try:
                st = src.stat()
            except FileNotFoundError:
                if note is not None and not note.removed:
                    note.removed = True
                    note.change_seq = next_change_seq()
                    note.save()
                    log.info('note %s removed at the source; its last copy is kept', path)
                return
            if (note is not None and not note.removed and arch.exists()
                    and st.st_size == note.seen_size and st.st_mtime_ns == note.seen_mtime_ns):
                return
            data = src.read_bytes()
            digest = 'sha256:' + hashlib.sha256(data).hexdigest()
            if note is None:
                note = Note(project=parts[0], path=path, archive_path=path)
            if note.pk is None or note.removed or note.digest != digest or not arch.exists():
                arch.parent.mkdir(parents=True, exist_ok=True)
                tmp = arch.with_name('.' + arch.name + '.memory-tmp')
                with open(tmp, 'wb') as out:
                    out.write(data)
                    out.flush()
                    os.fsync(out.fileno())
                os.replace(tmp, arch)
                note.digest = digest
                note.read_at = now
                note.removed = False
                note.change_seq = next_change_seq()
            note.seen_size = st.st_size
            note.seen_mtime_ns = st.st_mtime_ns
            note.save()
