"""The index's lag signal (Corpus Indexing REQ-05, REQ-01, REQ-09):
whether a retrieval issued now may be incomplete, and why.

``index_status()`` is the read Knowledge Retrieval REQ-07 consumes. It is
computed from what the indexer has recorded and from the sources'
own positions, not from the indexer's say-so, so it holds while the indexer
is stopped. The index is incomplete when any of these holds:

- a class has never been read to its end ("not indexed yet", REQ-01);
- an event asked for a catch-up that has not finished (REQ-04);
- an event stream holds entries the indexer has not read or not finished
  (``memory-indexer``'s lag and pending count; a stream with no such group
  yet counts in full);
- the archive's session listing or notes read is ahead of the indexer's
  cursor;
- a library file's upload was announced and its extraction is not recorded
  yet, or an extraction record differs from what the index built from it. A
  failed extraction is not lag (REQ-05; brief I-22): it stays the
  extraction feature's failure;
- a record failed to index (REQ-09).

A record lost in the relay's inherited window is not counted: nothing
announces it (REQ-05; §4).
"""

from __future__ import annotations

from django.conf import settings
from django.db.models import Max
from django.utils import timezone

from archive.models import ArchiveEpoch, Note, Session

from . import follow
from .events import ARTIFACT_STREAM, GROUP, WORK_ITEM_STREAMS
from .models import IndexCursor, IndexFailure, IndexState, PendingArtifact


def _archive_behind(name: str, model) -> bool:
    cursor = IndexCursor.objects.filter(name=name).values_list('cursor', flat=True).first() or ''
    newest = model.objects.aggregate(m=Max('change_seq'))['m'] or 0
    try:
        epoch, seq = cursor.split(':', 1)
        epoch, seq = str(epoch), int(seq)
    except ValueError:
        return newest > 0
    current = ArchiveEpoch.objects.filter(pk=1).values_list('epoch', flat=True).first()
    return str(current) != epoch or newest > seq


def _streams(client) -> dict:
    out = {}
    for stream in [ARTIFACT_STREAM] + sorted(client.scan_iter(match=WORK_ITEM_STREAMS, _type='STREAM')):
        if not client.exists(stream):
            continue
        group = next((g for g in client.xinfo_groups(stream) if g.get('name') == GROUP), None)
        if group is None:
            out[stream] = {'unread': client.xlen(stream), 'pending': 0}
            continue
        unread = group.get('lag')
        if unread is None:  # Redis cannot count it (entries deleted): behind if not at the stream's end
            info = client.xinfo_stream(stream)
            unread = 0 if group.get('last-delivered-id') == info.get('last-generated-id') else 1
        out[stream] = {'unread': int(unread), 'pending': int(group.get('pending') or 0)}
    return out


def index_status(redis_client=None) -> dict:
    """``{'incomplete': bool, 'reasons': [str], ...}``."""
    reasons = []
    cursors = {c.name: c for c in IndexCursor.objects.all()}
    for name in follow.FOLLOWED_READS:
        row = cursors.get(name)
        if row is None or not row.caught_up:
            reasons.append(f'{name}: not indexed yet')
        elif row.requested > row.completed:
            reasons.append(f'{name}: a catch-up is pending')

    if _archive_behind(follow.SESSIONS, Session):
        reasons.append('sessions: the archive has changes the index has not read')
    if _archive_behind(follow.NOTES, Note):
        reasons.append('notes: the archive has changes the index has not read')

    pending = PendingArtifact.objects.count()
    if pending:
        reasons.append(f'library: {pending} uploaded file(s) awaiting extraction')
    behind = len(follow.library_behind())
    if behind:
        reasons.append(f'library: {behind} extraction record(s) not indexed yet')

    failures = list(IndexFailure.objects.order_by('source_key').values_list('source_key', 'reason'))
    if failures:
        reasons.append(f'{len(failures)} record(s) failed to index')

    streams = {}
    try:
        if redis_client is None:
            import redis
            redis_client = redis.Redis.from_url(settings.MEMORY_REDIS_URL, decode_responses=True)
        streams = _streams(redis_client)
    except Exception as err:  # noqa: BLE001 - an unreadable stream is itself a reason
        reasons.append(f'event streams unreadable: {err}')
    for stream, counts in streams.items():
        if counts['unread'] or counts['pending']:
            reasons.append(f"{stream}: {counts['unread']} unread and {counts['pending']} unfinished event(s)")

    state = IndexState.objects.filter(pk=1).first()
    heartbeat = state.heartbeat_at if state else None
    return {
        'incomplete': bool(reasons),
        'reasons': reasons,
        'failures': [{'source': key, 'reason': reason} for key, reason in failures],
        'streams': streams,
        'pendingCatchUps': sorted(c.name for c in cursors.values() if c.requested > c.completed),
        'heartbeatAgeSeconds': (timezone.now() - heartbeat).total_seconds() if heartbeat else None,
        'platformCommit': state.platform_commit if state else '',
    }
