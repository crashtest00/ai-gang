"""Writing the index: a source's units are replaced, removed and failed as
one (Corpus Indexing REQ-02, REQ-07, REQ-08, REQ-09).

Source keys, one per source record whose units are replaced together:

    work_item:<id>                 a work item's fields and history
    comment:<id>                   a work-item comment
    delivery:<id>                  a delivery artifact
    a2a:<task id>                  an A2A Task's messages
    session:<session id>:<file>    one archived transcript of a session
    note:<path>                    a project's `memory/` note
    artifact:<id>                  a library file's extracted text
    platform:<path>                a document of the platform corpus

The scope is stamped here, from the class alone: `platform` for the
platform corpus and `project` for every other class (REQ-08). The database
refuses any other pairing (``index_unit_scope_matches_class``).
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterable, Optional

from django.db import connection, transaction
from django.db.models import F
from django.utils import timezone

from .models import (
    CLASS_COMMENT, CLASS_DELIVERY, CLASS_LIBRARY, CLASS_PLATFORM, CLASS_SPECIFICATION, CLASS_WORK_ITEM,
    SCOPE_PLATFORM, SCOPE_PROJECT, IndexCursor, IndexFailure, IndexSource, IndexUnit, SpecificationLink,
)

_WORK_ITEM_CLASSES = (CLASS_WORK_ITEM, CLASS_COMMENT, CLASS_DELIVERY)


def scope_for(source_class: str) -> str:
    return SCOPE_PLATFORM if source_class == CLASS_PLATFORM else SCOPE_PROJECT


def stamp_of(source_key: str) -> Optional[dict]:
    """What the source was last indexed from, or None. A source with a
    recorded failure has no stamp, so it is indexed again."""
    if IndexFailure.objects.filter(source_key=source_key).exists():
        return None
    row = IndexSource.objects.filter(pk=source_key).first()
    return row.stamp if row else None


def replace_units(source_key: str, source_class: str, stamp: dict, units: Iterable[dict], *,
                  work_item_id=None) -> int:
    """Replace a source's units with ``units`` (each ``reference``, ``text``
    and optionally ``written_at``/``current``), record what they were built
    from, and clear the source's failure, in one transaction."""
    now = timezone.now()
    scope = scope_for(source_class)
    rows = [IndexUnit(source_class=source_class, scope=scope, source_key=source_key, work_item_id=work_item_id,
                      reference=unit['reference'], text=unit['text'], position=position,
                      current=unit.get('current', True), written_at=unit.get('written_at'), indexed_at=now)
            for position, unit in enumerate(units)]
    with transaction.atomic():
        IndexUnit.objects.filter(source_key=source_key).delete()
        IndexUnit.objects.bulk_create(rows)
        IndexSource.objects.update_or_create(source_key=source_key, defaults={
            'source_class': source_class, 'stamp': stamp, 'indexed_at': now, 'work_item_id': work_item_id})
        IndexFailure.objects.filter(source_key=source_key).delete()
    return len(rows)


def remove_source(source_key: str) -> None:
    """REQ-07: a source deleted from its home leaves nothing in the index."""
    with transaction.atomic():
        IndexUnit.objects.filter(source_key=source_key).delete()
        IndexSource.objects.filter(pk=source_key).delete()
        IndexFailure.objects.filter(source_key=source_key).delete()


def remove_work_item(work_item_id) -> None:
    """REQ-07: a deleted work item takes its fields, history, comments and
    delivery artifacts with it, as Core's delete cascades to them."""
    with transaction.atomic():
        keys = set(IndexSource.objects.filter(work_item_id=work_item_id, source_class__in=_WORK_ITEM_CLASSES)
                   .values_list('source_key', flat=True))
        keys |= set(IndexUnit.objects.filter(work_item_id=work_item_id, source_class__in=_WORK_ITEM_CLASSES)
                    .values_list('source_key', flat=True))
        keys.add(f'work_item:{work_item_id}')
        IndexUnit.objects.filter(source_key__in=keys).delete()
        IndexSource.objects.filter(source_key__in=keys).delete()
        IndexFailure.objects.filter(source_key__in=keys).delete()
        # A failed comment or delivery artifact has no unit or source row to
        # name its work item; its failure does, so a retry cannot bring it back.
        IndexFailure.objects.filter(source_class__in=_WORK_ITEM_CLASSES,
                                    payload__workItemId=str(work_item_id)).delete()
        set_specification_link(work_item_id, None)


def _work_item_of(source_class: str, payload: dict) -> Optional[str]:
    """The work item a failed record of classes 2-4 belongs to, from the
    record its failure keeps for the retry."""
    record = payload.get('record') if isinstance(payload, dict) else None
    if source_class not in _WORK_ITEM_CLASSES or not isinstance(record, dict):
        return None
    work_item_id = record.get('id') if source_class == CLASS_WORK_ITEM else record.get('work_item_id')
    return str(work_item_id) if work_item_id else None


def record_failure(source_key: str, source_class: str, reason: str, payload: dict) -> None:
    """REQ-09: the record is named, with why, and removed from the index
    until a retry succeeds; it counts toward REQ-05's signal meanwhile. A
    record of a work item's classes keeps that work item's id in its
    payload, so deleting the work item removes the failure (REQ-07)."""
    now = timezone.now()
    work_item_id = _work_item_of(source_class, payload)
    if work_item_id is not None:
        payload = {**payload, 'workItemId': work_item_id}
    with transaction.atomic():
        IndexUnit.objects.filter(source_key=source_key).delete()
        IndexSource.objects.filter(pk=source_key).delete()
        updated = IndexFailure.objects.filter(source_key=source_key).update(
            reason=reason or 'unknown error', payload=payload, attempts=F('attempts') + 1, last_failed_at=now)
        if not updated:
            IndexFailure.objects.create(source_key=source_key, source_class=source_class,
                                        reason=reason or 'unknown error', payload=payload,
                                        first_failed_at=now, last_failed_at=now)


def library_class(artifact_id) -> str:
    """Class 1 when a work item's specification link names the file, else
    class 7 (REQ-01)."""
    named = SpecificationLink.objects.filter(artifact_id=artifact_id).exists()
    return CLASS_SPECIFICATION if named else CLASS_LIBRARY


def set_specification_link(work_item_id, artifact_id) -> None:
    """Record a work item's specification link as Core returned it, and
    re-class the library files whose class that changes."""
    with transaction.atomic():
        previous = SpecificationLink.objects.filter(work_item_id=work_item_id).first()
        old = previous.artifact_id if previous else None
        if (str(old) if old else None) == (str(artifact_id) if artifact_id else None):
            return
        if artifact_id is None:
            SpecificationLink.objects.filter(work_item_id=work_item_id).delete()
        else:
            SpecificationLink.objects.update_or_create(work_item_id=work_item_id,
                                                       defaults={'artifact_id': artifact_id})
        for changed in {a for a in (old, artifact_id) if a is not None}:
            key = f'artifact:{changed}'
            new_class = library_class(changed)
            IndexUnit.objects.filter(source_key=key).update(source_class=new_class)
            IndexSource.objects.filter(pk=key).update(source_class=new_class)


def request_catch_up(name: str) -> None:
    """An event asked for a catch-up of one followed read (REQ-04)."""
    IndexCursor.objects.get_or_create(name=name)
    IndexCursor.objects.filter(name=name).update(requested=F('requested') + 1)


# One writer at a time: memory-indexer's loop and a rebuild take this
# PostgreSQL advisory lock around their work, so a rebuild never interleaves
# with a catch-up.
INDEX_LOCK_KEY = 0x6D656D696E6478  # "meminx"


@contextmanager
def index_lock():
    with connection.cursor() as cur:
        cur.execute('SELECT pg_advisory_lock(%s)', [INDEX_LOCK_KEY])
    try:
        yield
    finally:
        with connection.cursor() as cur:
            cur.execute('SELECT pg_advisory_unlock(%s)', [INDEX_LOCK_KEY])
