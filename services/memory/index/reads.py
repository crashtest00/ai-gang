"""The owners' read interfaces the indexer reads every class through
(Corpus Indexing §4, "Where each class is read from"). It never connects
to Core's database or reads a shared volume.

Core (``MEMORY_CORE_URL``), each a paged, changed-since enumeration that
writes no access-log row:

    GET /work-items/changes          work items with history and links (REQ-11)
    GET /work-item-comments/changes  comments (REQ-11)
    GET /work-item-artifacts/changes delivery artifacts (REQ-11)
    GET /a2a/tasks                   A2A Tasks with their full record
                                     (Durable A2A Message Record REQ-11)

Memory's own archive, through memory-api (``MEMORY_API_URL``), never the
harness volume (REQ-01):

    GET /sessions?since=             sessions changed since (Session REQ-09)
    GET /sessions/<id>               a session's manifest (REQ-08)
    GET /sessions/<id>/file?path=    one archived file (REQ-08)
    GET /notes/changes?since=        notes changed since (REQ-08)
    GET /notes/file?path=            one archived note (REQ-08)

Library files are read from the extraction record in Memory's own database
(``index.follow``), and the platform corpus from the Source checkout
(``index.platform``).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from django.conf import settings

ACTOR = 'memory-indexer'
TIMEOUT_SECONDS = 60
PAGE_SIZE = 100

# name -> (route, the page's list key)
CORE_ENUMERATIONS = {
    'core:work_items': ('/work-items/changes', 'workItems'),
    'core:comments': ('/work-item-comments/changes', 'comments'),
    'core:delivery_artifacts': ('/work-item-artifacts/changes', 'deliveryArtifacts'),
    'core:a2a_tasks': ('/a2a/tasks', 'tasks'),
}


class SourceUnavailable(Exception):
    """An owner could not be read; the read is tried again later."""


class StaleCursor(Exception):
    """memory-api refused a cursor issued before its database was rebuilt
    (410): list again from no cursor."""


def _get(base: str, path: str, query: Optional[dict] = None):
    url = f'{base}{path}'
    if query:
        url += '?' + urllib.parse.urlencode(query)
    request = urllib.request.Request(url, headers={'X-Actor': ACTOR})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as err:
        if err.code in (404, 410):
            return err.code, err.read()
        raise SourceUnavailable(f'GET {url}: HTTP {err.code}') from err
    except (urllib.error.URLError, OSError) as err:
        raise SourceUnavailable(f'GET {url}: {err}') from err


def _json(body: bytes, what: str):
    try:
        return json.loads(body)
    except ValueError as err:
        raise SourceUnavailable(f'{what}: not JSON ({err})') from err


def core_page(name: str, cursor: str) -> tuple[list, str, bool]:
    """One page of a Core enumeration: ``(records, nextCursor, hasMore)``."""
    route, key = CORE_ENUMERATIONS[name]
    status, body = _get(settings.MEMORY_CORE_URL, route, {'cursor': cursor, 'limit': PAGE_SIZE})
    if status != 200:
        raise SourceUnavailable(f'{route}: HTTP {status}')
    page = _json(body, route)
    return page.get(key) or [], page.get('nextCursor') or '', bool(page.get('hasMore'))


def _changes(path: str, since: str) -> dict:
    status, body = _get(settings.MEMORY_API_URL, path, {'since': since} if since else None)
    if status == 410:
        raise StaleCursor(path)
    if status != 200:
        raise SourceUnavailable(f'{path}: HTTP {status}')
    return _json(body, path)


def sessions_changed(since: str) -> dict:
    return _changes('/sessions', since)


def notes_changed(since: str) -> dict:
    return _changes('/notes/changes', since)


def session_manifest(session_id: str) -> Optional[dict]:
    status, body = _get(settings.MEMORY_API_URL, f'/sessions/{urllib.parse.quote(session_id, safe="")}')
    return _json(body, 'session manifest') if status == 200 else None


def session_file(session_id: str, archive_path: str) -> Optional[bytes]:
    status, body = _get(settings.MEMORY_API_URL, f'/sessions/{urllib.parse.quote(session_id, safe="")}/file',
                        {'path': archive_path})
    return body if status == 200 else None


def note_file(archive_path: str) -> Optional[bytes]:
    status, body = _get(settings.MEMORY_API_URL, '/notes/file', {'path': archive_path})
    return body if status == 200 else None
