"""Indexing each class from its owner's read (Corpus Indexing REQ-01,
REQ-02, REQ-04, REQ-07, REQ-09, REQ-10), and the followed reads that keep
the index current and rebuild it (REQ-03, REQ-04).

Each record is indexed through ``_guarded``. A record that fails to index
is recorded as a failure, with its reason and what a retry needs, and is
absent from the index until ``retry_failures`` succeeds (REQ-09). An owner
that cannot be read at all (``reads.SourceUnavailable``) is not a record's
failure. The read stops where it is, its cursor unmoved, and is tried again,
and it stays visible as lag meanwhile (REQ-05).
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from typing import Callable, Optional

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from archive import harness
from extraction.models import OUTCOME_EXTRACTED, ExtractionRecord

from . import platform, reads, render, sessions, store
from .chunking import cut
from .models import (
    CLASS_A2A, CLASS_COMMENT, CLASS_DELIVERY, CLASS_PLATFORM, CLASS_SESSION, CLASS_WORK_ITEM, IndexCursor,
    IndexFailure, IndexSource, IndexState, IndexUnit, PendingArtifact,
)

log = logging.getLogger('memory.index')

SESSIONS = 'memory:sessions'
NOTES = 'memory:notes'
LIBRARY = 'library'
PLATFORM = 'platform'
EVENTS = 'events'  # an event-stream entry the indexer could not act on (REQ-09)
FOLLOWED_READS = tuple(reads.CORE_ENUMERATIONS) + (SESSIONS, NOTES, LIBRARY, PLATFORM)


def _digest(value) -> str:
    return 'sha256:' + hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _core_cuts(rendering: render.Rendering, reference: dict, written_at=None) -> list[dict]:
    """A Core record's units. Each reference names the fields of Core's read
    its text came from (``render.Rendering.fields``, Knowledge Retrieval
    REQ-04); a slice holding none of the record's text, only rendering, is
    no unit."""
    units = []
    for c in cut(rendering.text):
        fields = rendering.fields(c.offset, c.end)
        if fields:
            units.append({'text': rendering.text[c.offset:c.end], 'written_at': written_at,
                          'reference': {**reference, 'offset': c.offset, 'end': c.end, 'fields': fields}})
    return units


def _cuts_with_lines(text: str, reference: dict, written_at=None) -> list[dict]:
    return [{'text': text[c.offset:c.end], 'written_at': written_at,
             'reference': {**reference, 'lineStart': c.line_start, 'lineEnd': c.line_end,
                           'offset': c.offset, 'end': c.end}}
            for c in cut(text)]


def _guarded(source_key: str, source_class: str, payload: dict, index: Callable[[], None]) -> bool:
    try:
        index()
        return True
    except reads.SourceUnavailable:
        raise
    except Exception as err:  # noqa: BLE001 - REQ-09: any failure of one record is recorded, never skipped
        log.exception('indexing %s failed', source_key)
        store.record_failure(source_key, source_class, f'{type(err).__name__}: {err}', payload)
        return False


# --- Classes 2–5: Core's records ---------------------------------------------

def index_work_item(record: dict) -> None:
    work_item_id = record['id']
    sections = render.work_item(record)
    store.set_specification_link(work_item_id, (record.get('specification_link') or {}).get('artifact_id'))
    key = f'work_item:{work_item_id}'
    stamp = {'digest': _digest([(section, r.text, r.spans) for section, r in sections])}
    if store.stamp_of(key) == stamp:
        return
    written_at = record.get('updated_at') or record.get('created_at')
    units = []
    for section, rendering in sections:
        units += _core_cuts(rendering, {'workItemId': work_item_id, 'project': record.get('project'),
                                        'section': section}, written_at)
    store.replace_units(key, CLASS_WORK_ITEM, stamp, units, work_item_id=work_item_id)


def index_comment(record: dict) -> None:
    key = f"comment:{record['id']}"
    rendering = render.comment(record)
    stamp = {'digest': _digest([rendering.text, rendering.spans])}
    if store.stamp_of(key) == stamp:
        return
    units = _core_cuts(rendering, {'commentId': record['id'], 'workItemId': record['work_item_id'],
                                   'project': record.get('project')}, record.get('created_at'))
    store.replace_units(key, CLASS_COMMENT, stamp, units, work_item_id=record['work_item_id'])


def index_delivery_artifact(record: dict) -> None:
    key = f"delivery:{record['id']}"
    rendering = render.delivery_artifact(record)
    stamp = {'digest': _digest([rendering.text, rendering.spans])}
    if store.stamp_of(key) == stamp:
        return
    units = _core_cuts(rendering, {'deliveryArtifactId': record['id'], 'workItemId': record['work_item_id'],
                                   'project': record.get('project')}, record.get('created_at'))
    store.replace_units(key, CLASS_DELIVERY, stamp, units, work_item_id=record['work_item_id'])


def index_a2a_task(record: dict) -> None:
    """Each archived message of a Task, with its artifacts (class 5). A
    Task enumerated twice is indexed once: its messages are unchanged."""
    task_id = record['id']
    key = f'a2a:{task_id}'
    messages = [(m['messageId'], render.a2a_message(m), m.get('timestamp') or m.get('recordedAt'))
                for m in record.get('messages') or []]
    stamp = {'digest': _digest([(m, r.text, r.spans) for m, r, _ in messages])}
    if store.stamp_of(key) == stamp:
        return
    units = []
    for message_id, rendering, written_at in messages:
        units += _core_cuts(rendering, {'taskId': task_id, 'messageId': message_id,
                                        'project': record.get('project'), 'workItemId': record.get('workItemId')},
                            written_at)
    store.replace_units(key, CLASS_A2A, stamp, units)


_CORE_INDEXERS = {
    'core:work_items': (index_work_item, 'work_item', CLASS_WORK_ITEM),
    'core:comments': (index_comment, 'comment', CLASS_COMMENT),
    'core:delivery_artifacts': (index_delivery_artifact, 'delivery', CLASS_DELIVERY),
    'core:a2a_tasks': (index_a2a_task, 'a2a', CLASS_A2A),
}


def catch_up_core(name: str) -> int:
    """Read one Core enumeration from the stored cursor to the end of its
    round, indexing every record, and save the cursor page by page. Returns
    how many records were read."""
    cursor_row, _ = IndexCursor.objects.get_or_create(name=name)
    started_from = cursor_row.requested
    index, prefix, source_class = _CORE_INDEXERS[name]
    cursor, count = cursor_row.cursor, 0
    while True:
        records, next_cursor, has_more = reads.core_page(name, cursor)
        for record in records:
            key = f"{prefix}:{record['id']}"
            _guarded(key, source_class, {'read': name, 'record': record}, lambda r=record: index(r))
        count += len(records)
        IndexCursor.objects.filter(name=name).update(cursor=next_cursor)
        cursor = next_cursor
        if not has_more:
            break
    IndexCursor.objects.filter(name=name).update(completed=started_from, caught_up=True,
                                                 last_round_at=timezone.now())
    return count


def apply_deletion(data: dict) -> None:
    """REQ-07: Core's ``work_item.record_deleted`` outbox row, as the relay
    publishes it. Only records whose text the index holds remove units; a
    deleted specification link re-classes the file it named."""
    record_class, record_id = data.get('recordClass'), data.get('id')
    if record_class == 'work_item':
        store.remove_work_item(record_id)
    elif record_class == 'comment':
        store.remove_source(f'comment:{record_id}')
    elif record_class == 'delivery_artifact':
        store.remove_source(f'delivery:{record_id}')
    elif record_class == 'specification_link' and data.get('workItemId'):
        store.set_specification_link(data['workItemId'], None)


# --- Class 6: sessions and their projects' memory/ notes ----------------------

def _session_file_units(session_id: str, project: str, archive_path: str, data: bytes, upto_line: Optional[int],
                        subagent: Optional[dict]) -> list[dict]:
    units = []
    for unit in sessions.turn_units(data, upto_line=upto_line):
        reference = {'sessionId': session_id, 'project': project, 'file': archive_path, 'turn': unit.turn,
                     'lines': unit.lines, 'turnLines': unit.turn_lines, 'lineStart': unit.lines[0],
                     'lineEnd': unit.lines[-1], 'offset': unit.offset, 'end': unit.end}
        if subagent is not None:
            reference.update(subagent)
        units.append({'text': unit.text, 'reference': reference, 'written_at': unit.written_at})
    return units


def index_session(session_id: str) -> None:
    """Every turn of a session whose run is over: the run's end record is
    archived, or the session is no longer `active` (REQ-04; Session
    REQ-06). A subagent's turns follow the run of the tool call that
    started it."""
    manifest = reads.session_manifest(session_id)
    if manifest is None:
        return
    project, over = manifest.get('project'), manifest.get('status') != 'active'
    files = [f for f in manifest.get('files') or [] if f['archive_path'].endswith(harness.TRANSCRIPT_SUFFIX)]
    main = next((f for f in files if harness.is_session_transcript(tuple(f['archive_path'].split('/')))), None)
    if main is None:
        return
    main_data = reads.session_file(session_id, main['archive_path']) or b''
    end_line = sessions.last_end_line(main_data)
    for entry in files:
        path = entry['archive_path']
        key = f'session:{session_id}:{path}'
        payload = {'read': SESSIONS, 'sessionId': session_id}
        if path == main['archive_path']:
            def index_main(path=path, key=key):
                # Stamped by the units themselves, not the archive copy's
                # digest: an active session's copy grows with every record
                # its running turn writes, while its units (the turns up to
                # the last end record) stay as they were, and are not
                # written again (REQ-04's freshness bound).
                upto = None if over else end_line
                units = _session_file_units(session_id, project, path, main_data, upto, None)
                stamp = {'units': _digest(units), 'upto': upto}
                if store.stamp_of(key) != stamp:
                    store.replace_units(key, CLASS_SESSION, stamp, units)
            _guarded(key, CLASS_SESSION, payload, index_main)
        elif harness.is_subagent_transcript(path):
            _guarded(key, CLASS_SESSION, payload, lambda e=entry, k=key: _index_subagent(
                session_id, project, e, k, main_data, end_line, over))


def entry_digest(entry: dict) -> str:
    return entry.get('digest') or ''


def _index_subagent(session_id, project, entry, key, main_data, end_line, over) -> None:
    path = entry['archive_path']
    meta = reads.session_file(session_id, harness.subagent_meta_path(path))
    tool_use_id = harness.subagent_tool_use_id(meta) if meta is not None else None
    if tool_use_id is None:
        raise ValueError(f'{harness.subagent_meta_path(path)} names no toolUseId, so this subagent\'s units '
                         'cannot carry the tool call that started it (REQ-10)')
    started_at = sessions.tool_use_line(main_data, tool_use_id)
    ready = over or (started_at is not None and started_at <= end_line)
    stamp = {'digest': entry_digest(entry), 'ready': ready}
    if store.stamp_of(key) == stamp:
        return
    data = reads.session_file(session_id, path) if ready else None
    units = _session_file_units(session_id, project, path, data, None,
                                {'parentSessionId': session_id, 'toolUseId': tool_use_id}) if data else []
    store.replace_units(key, CLASS_SESSION, stamp, units)


def index_note(note: dict) -> None:
    """A `memory/` note, cut from its text (REQ-10: not a turn). A replaced
    note's units replace the earlier ones; a removed note's are removed."""
    key = f"note:{note['path']}"
    if note.get('removed'):
        store.remove_source(key)
        return
    if store.stamp_of(key) == {'digest': note.get('digest')}:
        return
    data = reads.note_file(note['archive_path'])
    if data is None:
        raise FileNotFoundError(f"memory-api has no archived copy of {note['path']}")
    digest = 'sha256:' + hashlib.sha256(data).hexdigest()
    reference = {'project': note.get('project'), 'path': note['path'], 'archivePath': note['archive_path'],
                 'digest': digest}
    store.replace_units(key, CLASS_SESSION, {'digest': digest},
                        _cuts_with_lines(data.decode('utf-8', 'replace'), reference, note.get('read_at')))


def _follow_archive(name: str, list_changes, items_key: str, index_item, key_of) -> int:
    cursor_row, _ = IndexCursor.objects.get_or_create(name=name)
    started_from, cursor, count = cursor_row.requested, cursor_row.cursor, 0
    while True:
        try:
            page = list_changes(cursor)
        except reads.StaleCursor:
            # Memory's database was rebuilt since this cursor was issued
            # (DR-6, DR-7): list again from the beginning.
            log.warning('%s: cursor refused as stale; listing again from no cursor', name)
            cursor = ''
            IndexCursor.objects.filter(name=name).update(cursor='')
            continue
        for item in page.get(items_key) or []:
            _guarded(key_of(item), CLASS_SESSION, {'read': name, 'item': item}, lambda i=item: index_item(i))
        count += len(page.get(items_key) or [])
        cursor = page.get('cursor') or cursor
        IndexCursor.objects.filter(name=name).update(cursor=cursor)
        if not page.get('more'):
            break
    IndexCursor.objects.filter(name=name).update(completed=started_from, caught_up=True,
                                                 last_round_at=timezone.now())
    return count


def follow_sessions() -> int:
    return _follow_archive(SESSIONS, reads.sessions_changed, 'sessions', index_session,
                           lambda sid: f'session:{sid}')


def follow_notes() -> int:
    return _follow_archive(NOTES, reads.notes_changed, 'notes', index_note, lambda n: f"note:{n['path']}")


# --- Classes 1 and 7: library files, from the extraction record ---------------

def _stamp(updated_at, sha256) -> dict:
    return {'updatedAt': updated_at.isoformat(), 'sha256': sha256}


def library_behind() -> list[str]:
    """Artifacts whose extraction record differs from what the index last
    built from them: a new or replaced extraction, or a failure whose
    earlier text is still indexed."""
    indexed = {key[len('artifact:'):]: stamp for key, stamp in
               IndexSource.objects.filter(source_key__startswith='artifact:').values_list('source_key', 'stamp')}
    failed = set(IndexFailure.objects.filter(source_key__startswith='artifact:')
                 .values_list('source_key', flat=True))
    behind = []
    for artifact_id, updated_at, sha, outcome in ExtractionRecord.objects.values_list(
            'artifact_id', 'source_updated_at', 'source_sha256', 'outcome'):
        artifact_id = str(artifact_id)
        want = {**_stamp(updated_at, sha), 'outcome': outcome}
        if indexed.get(artifact_id) != want or f'artifact:{artifact_id}' in failed:
            behind.append(artifact_id)
    return behind


def index_library_file(artifact_id: str) -> None:
    """A library file's units from its retained extraction record, never
    its bytes (REQ-01). A failed extraction contributes no units; it stays
    the extraction feature's recorded failure (its REQ-06). When a later
    upload of the file is already announced, this extraction is no longer
    current and its units are written as not current (REQ-04)."""
    key = f'artifact:{artifact_id}'
    record = ExtractionRecord.objects.filter(artifact_id=artifact_id).first()
    if record is None:
        return
    stamp = _stamp(record.source_updated_at, record.source_sha256)
    units = []
    if record.outcome == OUTCOME_EXTRACTED:
        reference = {'artifactId': artifact_id, 'stamp': stamp, 'format': record.format}
        superseded = PendingArtifact.objects.filter(artifact_id=artifact_id,
                                                    updated_at__gt=record.source_updated_at).exists()
        units = [{**unit, 'current': not superseded}
                 for unit in _cuts_with_lines(record.text or '', reference, record.source_updated_at)]
    store.replace_units(key, store.library_class(artifact_id), {**stamp, 'outcome': record.outcome}, units)


def follow_library() -> int:
    behind = library_behind()
    for artifact_id in behind:
        _guarded(f'artifact:{artifact_id}', store.library_class(artifact_id), {'read': LIBRARY,
                 'artifactId': artifact_id}, lambda a=artifact_id: index_library_file(a))
    _settle_pending()
    IndexCursor.objects.get_or_create(name=LIBRARY)
    IndexCursor.objects.filter(name=LIBRARY).update(caught_up=True, last_round_at=timezone.now())
    return len(behind)


def _settle_pending() -> None:
    """A pending upload is settled once its extraction is recorded, either
    way: a failed extraction is not lag (REQ-05)."""
    for pending in PendingArtifact.objects.all():
        record = ExtractionRecord.objects.filter(artifact_id=pending.artifact_id).first()
        if record is not None and record.source_updated_at >= pending.updated_at:
            PendingArtifact.objects.filter(pk=pending.pk, updated_at=pending.updated_at).delete()


def note_upload(artifact_id: str, uploaded_at, action: str) -> None:
    """An ``artifact.uploaded`` event: the file is "not indexed yet" until
    its extraction is recorded (REQ-05), and after a re-upload its earlier
    units are no longer current (REQ-04). Only an upload later than the one
    the units were built from does that: an earlier upload's event read late
    leaves the current units current."""
    with transaction.atomic():
        pending = PendingArtifact.objects.select_for_update().filter(artifact_id=artifact_id).first()
        if pending is None:
            PendingArtifact.objects.create(artifact_id=artifact_id, updated_at=uploaded_at, action=action,
                                           received_at=timezone.now())
        elif uploaded_at > pending.updated_at:
            pending.updated_at, pending.action = uploaded_at, action
            pending.save(update_fields=['updated_at', 'action'])
        source = IndexSource.objects.filter(pk=f'artifact:{artifact_id}').first()
        indexed_from = source.stamp.get('updatedAt') if source is not None else None
        if source is not None and (not indexed_from or uploaded_at > datetime.fromisoformat(indexed_from)):
            IndexUnit.objects.filter(source_key=f'artifact:{artifact_id}').update(current=False)
    _settle_pending()


# --- The platform corpus (REQ-08) ---------------------------------------------

def follow_platform(*, force: bool = False) -> int:
    """Index the platform corpus at the checkout's commit, when it has not
    been indexed at that commit yet (or on a rebuild)."""
    root = settings.MEMORY_PLATFORM_CORPUS_ROOT
    key = f'platform:{root}'
    try:
        commit = platform.head_commit(root)
        state, _ = IndexState.objects.get_or_create(pk=1)
        if commit == state.platform_commit and not force:
            return 0
        documents = platform.documents(root, commit)
        written = _platform_written_times(root, commit)
    except platform.PlatformUnavailable as err:
        _platform_failed(key, str(err))
        return 0
    count = 0
    try:
        with transaction.atomic():
            # Each document's units replace its own, so a passage whose text
            # is unchanged at the new commit keeps its embedding
            # (store.replace_units); a document gone at this commit goes.
            keys = set()
            for path, text in documents:
                keys.add(f'platform:{path}')
                count += store.replace_units(f'platform:{path}', CLASS_PLATFORM, {'commit': commit},
                                             _cuts_with_lines(text, {'commit': commit, 'path': path},
                                                              written.get(path, written[None])))
            IndexUnit.objects.filter(source_class=CLASS_PLATFORM).exclude(source_key__in=keys).delete()
            IndexSource.objects.filter(source_class=CLASS_PLATFORM).exclude(source_key__in=keys).delete()
            IndexState.objects.filter(pk=1).update(platform_commit=commit)
            IndexFailure.objects.filter(source_key=key).delete()
            IndexCursor.objects.get_or_create(name=PLATFORM)
            IndexCursor.objects.filter(name=PLATFORM).update(caught_up=True, last_round_at=timezone.now())
    except Exception as err:  # noqa: BLE001 - REQ-09: e.g. the embedding model cannot run; recorded, retried
        log.exception('indexing the platform corpus failed')
        _platform_failed(key, f'{type(err).__name__}: {err}')
        return 0
    return count


def _platform_failed(key: str, reason: str) -> None:
    """REQ-09: the platform corpus is one record, keyed by its checkout, while
    its units are keyed by document. A failure removes them all, so no
    passage of an earlier commit is served while it stands, and forgets the
    commit they were built from, so the next start or retry reads the
    checkout again."""
    with transaction.atomic():
        IndexUnit.objects.filter(source_class=CLASS_PLATFORM).delete()
        IndexSource.objects.filter(source_class=CLASS_PLATFORM).delete()
        IndexState.objects.filter(pk=1).update(platform_commit='')
        store.record_failure(key, CLASS_PLATFORM, reason, {'read': PLATFORM})


def _platform_written_times(root: str, commit: str) -> dict:
    """When each platform document was written (Knowledge Retrieval REQ-05):
    the committer time of the last commit at or before ``commit`` that
    touched its path, read from the checkout's own repository as
    ``platform.documents`` reads it. Key ``None`` holds ``commit``'s own
    committer time, for a path the history does not name (a shallow clone's
    boundary, say)."""
    from datetime import datetime  # local, so this section's edits stay inside it
    head = platform._git(root, 'show', '-s', '--format=%cI', commit).decode().strip()
    times = {None: datetime.fromisoformat(head)}
    # One pass over the history, newest first: a path's first appearance is its last change.
    # Each commit is "\x01<time>\0" then its paths, each ending in "\0".
    log = platform._git(root, 'log', '-z', '--format=%x01%cI', '--name-only', commit, '--', *platform.ROOTS)
    for entry in log.decode('utf-8', 'replace').split('\x01')[1:]:
        when, _, paths = entry.partition('\0')
        at = datetime.fromisoformat(when.strip())
        for path in paths.lstrip('\n').split('\0'):
            if path and path not in times:
                times[path] = at
    return times


# --- REQ-09: retry --------------------------------------------------------------

def retry_failures(source_key: Optional[str] = None) -> int:
    """Index each failed record again from what its failure recorded, without
    rebuilding the index. Returns how many now succeed."""
    failures = IndexFailure.objects.order_by('last_failed_at')
    if source_key is not None:
        failures = failures.filter(source_key=source_key)
    fixed = 0
    for failure in list(failures):
        payload, read = failure.payload, failure.payload.get('read')
        if read in _CORE_INDEXERS:
            ok = _guarded(failure.source_key, failure.source_class, payload,
                          lambda: _CORE_INDEXERS[read][0](payload['record']))
        elif read == SESSIONS:
            ok = _guarded(failure.source_key, failure.source_class, payload,
                          lambda: index_session(payload['sessionId'] if 'sessionId' in payload else payload['item']))
        elif read == NOTES:
            ok = _guarded(failure.source_key, failure.source_class, payload, lambda: index_note(payload['item']))
        elif read == LIBRARY:
            ok = _guarded(failure.source_key, failure.source_class, payload,
                          lambda: index_library_file(payload['artifactId']))
        elif read == PLATFORM:
            follow_platform(force=True)
            ok = not IndexFailure.objects.filter(source_key=failure.source_key).exists()
        elif read == EVENTS:
            from . import events
            ok = _guarded(failure.source_key, failure.source_class, payload,
                          lambda: events.apply(payload['stream'], payload['fields']))
            if ok:
                IndexFailure.objects.filter(source_key=failure.source_key).delete()
        else:
            continue
        fixed += 1 if ok and not IndexFailure.objects.filter(source_key=failure.source_key).exists() else 0
    return fixed


def rebuild() -> dict:
    """REQ-03: drop every unit and everything the index recorded about its
    sources, then read every class again from its owner, from no cursor.
    Library files are rebuilt from the retained extraction records, never by
    parsing a stored file again."""
    with transaction.atomic():
        for model in (IndexUnit, IndexSource, IndexFailure, IndexCursor, IndexState):
            model.objects.all().delete()
        from .models import SpecificationLink
        SpecificationLink.objects.all().delete()
    counts = {name: catch_up_core(name) for name in reads.CORE_ENUMERATIONS}
    counts[SESSIONS] = follow_sessions()
    counts[NOTES] = follow_notes()
    counts[LIBRARY] = follow_library()
    counts[PLATFORM] = follow_platform(force=True)
    return counts
