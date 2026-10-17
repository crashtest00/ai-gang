"""Core's artifact reads, over HTTP (artifact-text-extraction.md REQ-01, REQ-04).

Memory never connects to Core's database; it reads what any client reads:

    GET /artifacts/<id>?record=true   the record (``updatedAt`` is the stamp)
    GET /artifacts/<id>               the bytes, as stored, by canonical id
    GET /artifacts?cursor=&limit=     the read-only listing of id and
                                      ``updatedAt`` (REQ-04, Decision 2)

The listing is track 1's half of REQ-04 (``feat/v6.1-core-enumeration``).
``list_artifacts`` is the one function that knows its shape, which follows
Core's existing paged enumeration (``GET /a2a/tasks``: ``nextCursor``,
``hasMore``) until that route is merged.

Every request names this reader in ``X-Actor``, so Core's access log shows
whose reads they were.
"""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

from django.conf import settings

ACTOR = 'memory-extraction'
TIMEOUT_SECONDS = 30
CHUNK = 1024 * 1024


class CoreUnavailable(Exception):
    """Core could not be read; the job is released and tried again."""


@dataclass(frozen=True)
class ArtifactRecord:
    artifact_id: str
    updated_at: datetime
    original_filename: str


class FileMissing(Exception):
    """Core has the record, but the stored file is missing from the volume."""


def _url(path: str) -> str:
    return f'{settings.MEMORY_CORE_URL}{path}'


def _open(path: str):
    request = urllib.request.Request(_url(path), headers={'X-Actor': ACTOR})
    return urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS)


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def get_record(artifact_id) -> Optional[ArtifactRecord]:
    """The artifact's present record, or None when Core has no such artifact."""
    try:
        with _open(f'/artifacts/{artifact_id}?record=true') as response:
            body = json.loads(response.read())
    except urllib.error.HTTPError as err:
        if err.code == 404:
            return None
        raise CoreUnavailable(f'record read for {artifact_id}: HTTP {err.code}') from err
    except (urllib.error.URLError, OSError, ValueError) as err:
        raise CoreUnavailable(f'record read for {artifact_id}: {err}') from err
    return ArtifactRecord(artifact_id=str(body['id']), updated_at=parse_time(body['updatedAt']),
                          original_filename=body.get('originalFilename') or '')


def read_bytes(artifact_id, destination: Optional[Path]) -> str:
    """Stream the stored bytes by canonical id, return their SHA-256, and
    write them to ``destination`` when one is given (None: hash only, for
    the sweep). Reading never changes the stored file (REQ-01)."""
    digest = hashlib.sha256()
    try:
        with _open(f'/artifacts/{artifact_id}') as response:
            sink = open(destination, 'wb') if destination is not None else None
            try:
                while True:
                    chunk = response.read(CHUNK)
                    if not chunk:
                        break
                    digest.update(chunk)
                    if sink is not None:
                        sink.write(chunk)
            finally:
                if sink is not None:
                    sink.close()
    except urllib.error.HTTPError as err:
        if err.code == 404:
            try:
                body = json.loads(err.read() or b'{}')
            except ValueError:
                body = {}
            if body.get('error') == 'artifact file missing':
                raise FileMissing(body.get('path') or '') from err
            raise FileNotFoundError(artifact_id) from err
        raise CoreUnavailable(f'byte read for {artifact_id}: HTTP {err.code}') from err
    except (urllib.error.URLError, OSError) as err:
        raise CoreUnavailable(f'byte read for {artifact_id}: {err}') from err
    return digest.hexdigest()


def list_artifacts(page_size: int = 200) -> Iterator[tuple[str, datetime]]:
    """Every artifact in the library as ``(id, updatedAt)``, page by page."""
    cursor = ''
    while True:
        query = urllib.parse.urlencode({'cursor': cursor, 'limit': page_size})
        try:
            with _open(f'/artifacts?{query}') as response:
                page = json.loads(response.read())
        except (urllib.error.URLError, OSError, ValueError) as err:
            raise CoreUnavailable(f'artifact listing: {err}') from err
        for item in page.get('artifacts', []):
            yield str(item['id']), parse_time(item['updatedAt'])
        cursor = page.get('nextCursor') or ''
        if not page.get('hasMore') or not cursor:
            return
