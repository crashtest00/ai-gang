"""A result's provenance and the source's current status (Knowledge
Retrieval REQ-04, REQ-05).

``address`` is the canonical address a passage's reference resolves through
(REQ-04): the owner's own read, as the indexer read it
(``index.reads``): Core's reads for the four Core-owned classes and library
files, memory-api for sessions and notes, the Source checkout's commit for the
platform corpus.

``source_status`` is read from Core when the query runs, never from the
index (REQ-05): a work item's current status, an A2A message's ScrumMaster
verdict and reason (or that it has none yet), a library file's currency
against the artifact's present record. These reads use the actor
``memory-retrieval`` and are the only thing retrieval does to Core; Core logs
each as it logs any read (watch-list I-25), and a query makes one read per
distinct source, not one per passage.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Optional

from django.conf import settings

from index.models import (
    CLASS_A2A, CLASS_COMMENT, CLASS_DELIVERY, CLASS_LIBRARY, CLASS_PLATFORM, CLASS_SESSION, CLASS_SPECIFICATION,
    CLASS_WORK_ITEM,
)

ACTOR = 'memory-retrieval'

# What a reference must carry for its class to resolve (REQ-04). The index's
# CHECK constraints (``index_unit_reference_*``) refuse a unit without these;
# ``resolvable`` is the same rule on the query path, so a unit that somehow
# lacked one is not returned.
_REQUIRED = {
    CLASS_WORK_ITEM: ('workItemId',),
    CLASS_COMMENT: ('workItemId', 'commentId'),
    CLASS_DELIVERY: ('workItemId', 'deliveryArtifactId'),
    CLASS_A2A: ('taskId', 'messageId'),
    CLASS_LIBRARY: ('artifactId', 'stamp'),
    CLASS_SPECIFICATION: ('artifactId', 'stamp'),
    CLASS_PLATFORM: ('commit', 'path'),
}


def resolvable(source_class: str, reference: dict) -> bool:
    if source_class == CLASS_SESSION:
        # A note resolves through memory-api's read of its archived copy, so
        # it needs that copy's path (``address``), not only the note's own.
        return bool(reference.get('sessionId') and reference.get('file')) or bool(
            reference.get('project') and reference.get('path') and reference.get('archivePath')
            and reference.get('digest'))
    required = _REQUIRED.get(source_class)
    return required is not None and all(reference.get(key) for key in required)


def address(source_class: str, reference: dict) -> dict:
    """Where the passage's source is read in full, and where in it the
    passage lies."""
    location = {key: reference[key] for key in ('section', 'offset', 'end', 'lineStart', 'lineEnd', 'turn',
                                                'lines') if key in reference}
    if source_class == CLASS_WORK_ITEM:
        return {'owner': 'core', 'read': f"/work-items/{reference['workItemId']}?full=true", 'location': location}
    if source_class in (CLASS_COMMENT, CLASS_DELIVERY):
        return {'owner': 'core', 'read': f"/work-items/{reference['workItemId']}?full=true",
                'record': reference.get('commentId') or reference.get('deliveryArtifactId'), 'location': location}
    if source_class == CLASS_A2A:
        return {'owner': 'core', 'read': f"/a2a/tasks/{reference['taskId']}", 'record': reference['messageId'],
                'location': location}
    if source_class in (CLASS_LIBRARY, CLASS_SPECIFICATION):
        return {'owner': 'core', 'read': f"/artifacts/{reference['artifactId']}", 'stamp': reference['stamp'],
                'location': location}
    if source_class == CLASS_SESSION:
        if 'sessionId' in reference:
            return {'owner': 'memory-api',
                    'read': f"/sessions/{reference['sessionId']}/file?path={urllib.parse.quote(reference['file'])}",
                    'location': location}
        return {'owner': 'memory-api', 'read': f"/notes/file?path={urllib.parse.quote(reference['archivePath'])}",
                'path': reference['path'], 'digest': reference['digest'], 'location': location}
    return {'owner': 'source-checkout', 'read': f"git show {reference['commit']}:{reference['path']}",
            'location': location}


class CoreReads:
    """One query's reads of Core, each made once."""

    def __init__(self):
        self._cache: dict = {}

    def get(self, path: str):
        """``(status, body)``; ``(None, reason)`` when Core cannot be read."""
        if path not in self._cache:
            url = f'{settings.MEMORY_CORE_URL}{path}'
            request = urllib.request.Request(url, headers={'X-Actor': ACTOR})
            try:
                with urllib.request.urlopen(request, timeout=settings.MEMORY_RETRIEVAL_STATUS_TIMEOUT_SECONDS) as r:
                    self._cache[path] = (r.status, json.loads(r.read()))
            except urllib.error.HTTPError as err:
                self._cache[path] = (err.code, None) if err.code == 404 else (None, f'HTTP {err.code}')
            except (urllib.error.URLError, OSError, ValueError) as err:
                self._cache[path] = (None, str(err))
        return self._cache[path]


def _same_instant(a: str, b: str) -> bool:
    try:
        return datetime.fromisoformat(str(a)) == datetime.fromisoformat(str(b))
    except ValueError:
        return a == b


def source_status(source_class: str, reference: dict, core: CoreReads) -> Optional[dict]:
    """The source record's current status where it has one, as Core returns
    it now; ``None`` where the class has none (sessions, the platform
    corpus). ``{'state': 'unavailable', ...}`` when Core cannot be read."""
    if source_class in (CLASS_WORK_ITEM, CLASS_COMMENT, CLASS_DELIVERY):
        status, body = core.get(f"/work-items/{reference['workItemId']}")
        if status is None:
            return {'kind': 'work_item', 'state': 'unavailable', 'reason': body}
        if status == 404:
            return {'kind': 'work_item', 'state': 'deleted', 'workItemId': reference['workItemId']}
        return {'kind': 'work_item', 'state': 'known', 'workItemId': reference['workItemId'],
                'status': body.get('status'), 'updatedAt': body.get('updated_at')}
    if source_class == CLASS_A2A:
        status, body = core.get(f"/a2a/tasks/{reference['taskId']}")
        if status is None:
            return {'kind': 'a2a_message', 'state': 'unavailable', 'reason': body}
        if status == 404:
            return {'kind': 'a2a_message', 'state': 'deleted', 'taskId': reference['taskId']}
        message = next((m for m in body.get('messages') or [] if m.get('messageId') == reference['messageId']), None)
        verdict = (message or {}).get('verdict')
        return {'kind': 'a2a_message', 'state': 'known', 'taskId': reference['taskId'],
                'taskState': body.get('state'), 'verdict': verdict,
                'verdictRecorded': verdict is not None,
                'reason': (message or {}).get('reason'), 'outcome': (message or {}).get('outcome'),
                'note': None if verdict is not None else 'ScrumMaster has not yet recorded a verdict on this message'}
    if source_class in (CLASS_LIBRARY, CLASS_SPECIFICATION):
        status, body = core.get(f"/artifacts/{reference['artifactId']}?record=true")
        if status is None:
            return {'kind': 'library_file', 'state': 'unavailable', 'current': None, 'reason': body}
        if status == 404:
            return {'kind': 'library_file', 'state': 'deleted', 'current': False}
        current = _same_instant(body.get('updatedAt'), reference['stamp'].get('updatedAt'))
        return {'kind': 'library_file', 'state': 'known', 'current': current, 'artifactUpdatedAt': body.get('updatedAt'),
                'extractionUpdatedAt': reference['stamp'].get('updatedAt')}
    return None
