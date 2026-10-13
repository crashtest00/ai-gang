"""memory-api's reads (Durable Agent Session Record REQ-08, REQ-09).

- `GET /sessions/<session_id>` — a session's manifest.
- `GET /sessions/<session_id>/file?path=<archive_path>` — one archived
  file's bytes, so a consumer that wants only the manifest does not move
  the transcript.
- `GET /sessions[?since=<cursor>]` — REQ-09: the session ids whose manifest
  changed since a cursor (new bytes, a new file, a change of status), and
  the cursor to pass next.
- `GET /notes?project=<project>` — a project's archived `memory/` notes.
- `GET /notes/file?path=<archive_path>` — one note's archived bytes.
- `GET /notes/changes[?since=<cursor>]` — notes changed since a cursor,
  across every project: a first copy, a replaced copy, a note marked
  removed.
- `GET /counts` — per harness version, REQ-05's unknown and unparseable
  totals and REQ-06's transitions to quiet.

Every read is logged. A cursor carries the database's epoch; one issued
before the database was rebuilt is refused with 410, and the caller lists
again from no cursor (§4). Memory adds no caller check (REQ-08).
"""

import functools
import logging
import uuid
from pathlib import Path

from django.conf import settings
from django.http import FileResponse, JsonResponse

from .models import ArchiveEpoch, HarnessVersionTotals, Note, Session, SessionFile

log = logging.getLogger('memory.reads')
PAGE = 1000


def logged(view):
    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        response = view(request, *args, **kwargs)
        log.info('read %s %s%s -> %s', request.method, request.path,
                 ('?' + request.META['QUERY_STRING']) if request.META.get('QUERY_STRING') else '',
                 response.status_code)
        return response
    return wrapper


def _error(status, message):
    return JsonResponse({'error': message}, status=status)


def _epoch():
    # Written by migration 0001. A database that lost the row is a new
    # database too, so creating a fresh epoch refuses every older cursor.
    return ArchiveEpoch.objects.get_or_create(pk=1)[0].epoch


class _StaleCursor(Exception):
    pass


def _parse_cursor(raw):
    """`<epoch>:<seq>` → seq; None for no cursor."""
    if raw in (None, ''):
        return 0
    try:
        epoch, seq = raw.split(':', 1)
        epoch, seq = uuid.UUID(epoch), int(seq)
    except ValueError:
        raise ValueError('malformed cursor')
    if epoch != _epoch():
        raise _StaleCursor()
    return seq


def _cursor(seq):
    return f'{_epoch()}:{seq}'


def _since(request):
    try:
        return _parse_cursor(request.GET.get('since')), None
    except _StaleCursor:
        return None, _error(410, 'this cursor was issued before Memory\'s database was rebuilt; '
                                 'list again with no cursor')
    except ValueError as exc:
        return None, _error(400, str(exc))


def _archive_file(archive_path):
    root = Path(settings.MEMORY_ARCHIVE_ROOT).resolve()
    full = (root / archive_path).resolve()
    if root not in full.parents:
        return None
    return full


def _iso(value):
    return value.isoformat() if value else None


def manifest(session):
    files = []
    for f in session.files.order_by('path'):
        entry = {'path': f.path, 'archive_path': f.archive_path, 'bytes_read': f.bytes_read, 'digest': f.digest}
        if f.path.endswith('.jsonl'):
            entry['partial_tail'] = f.partial_tail
        files.append(entry)
    return {
        'session_id': session.session_id,
        'project': session.project,
        'source_path': session.source_path,
        'files': files,
        'first_seen': _iso(session.first_seen),
        'last_growth': _iso(session.last_growth),
        'harness_versions': session.harness_versions,
        'status': session.status,
        'record_counts': session.record_counts,
        'quiet_by': session.quiet_by,
        'pull_requests': [
            {'repository': p.repository, 'number': p.number, 'url': p.url, 'first_seen': p.first_seen}
            for p in session.pull_requests.order_by('id')],
        'commits': [{'hash': c.hash, 'role': c.role, 'time': c.time} for c in session.commits.order_by('id')],
    }


def note_record(note):
    return {'project': note.project, 'path': note.path, 'archive_path': note.archive_path,
            'digest': note.digest, 'read_at': _iso(note.read_at), 'removed': note.removed}


@logged
def health(request):
    return JsonResponse({'status': 'ok'})


@logged
def session_manifest(request, session_id):
    session = Session.objects.filter(pk=session_id).first()
    if session is None:
        return _error(404, f'no session {session_id}')
    return JsonResponse(manifest(session))


@logged
def session_file(request, session_id):
    path = request.GET.get('path', '')
    if not SessionFile.objects.filter(session_id=session_id, archive_path=path).exists():
        return _error(404, f'session {session_id} has no archived file {path!r}')
    full = _archive_file(path)
    if full is None or not full.is_file():
        return _error(404, f'archived file {path!r} is missing')
    return FileResponse(open(full, 'rb'), content_type='application/octet-stream')


@logged
def session_changes(request):
    seq, error = _since(request)
    if error:
        return error
    rows = list(Session.objects.filter(change_seq__gt=seq).order_by('change_seq')
                .values_list('session_id', 'change_seq')[:PAGE])
    last = rows[-1][1] if rows else seq
    return JsonResponse({'sessions': [r[0] for r in rows], 'cursor': _cursor(last), 'more': len(rows) == PAGE})


@logged
def notes_list(request):
    project = request.GET.get('project')
    if not project:
        return _error(400, 'name a project: ?project=<name>')
    return JsonResponse({'project': project,
                         'notes': [note_record(n) for n in Note.objects.filter(project=project).order_by('path')]})


@logged
def note_file(request):
    path = request.GET.get('path', '')
    if not Note.objects.filter(archive_path=path).exists():
        return _error(404, f'no archived note {path!r}')
    full = _archive_file(path)
    if full is None or not full.is_file():
        return _error(404, f'archived note {path!r} is missing')
    return FileResponse(open(full, 'rb'), content_type='application/octet-stream')


@logged
def note_changes(request):
    seq, error = _since(request)
    if error:
        return error
    notes = list(Note.objects.filter(change_seq__gt=seq).order_by('change_seq')[:PAGE])
    last = notes[-1].change_seq if notes else seq
    return JsonResponse({'notes': [note_record(n) for n in notes], 'cursor': _cursor(last),
                         'more': len(notes) == PAGE})


@logged
def counts(request):
    return JsonResponse({'versions': {
        t.version: {'unknown': t.unknown, 'unparseable': t.unparseable,
                    'quiet_end_record': t.quiet_end_record, 'quiet_interval': t.quiet_interval}
        for t in HarnessVersionTotals.objects.order_by('version')}})
