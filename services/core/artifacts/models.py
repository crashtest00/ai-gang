"""
The artifact record — artifact-ingress.md REQ-01.

Identity and nothing else: the canonical id, the path the file was stored
at, the original filename, the uploading actor, and the times. No type, no
status, no size class, no content-derived field of any kind (PRD §8,
"custody without interpretation").

Tables live in the same Postgres instance as canonical work items so the
work-item side's "does this artifact id resolve" check is a local read
(spec §4), but they are this app's own tables — ``artifact`` and
``artifact_access_log``, not ``work_item_*``.
"""

from __future__ import annotations

import uuid

from django.db import models
from django.utils import timezone

from . import storage


class Artifact(models.Model):
    """One uploaded file.

    ``file`` is a ``FileField`` whose ``name`` IS the recorded path
    (``db_column='path'``) — one column, so a record and the storage layer
    can never hold two different answers to "where is it". The bytes are
    never copied into the database: REQ-02 makes the file on the volume
    the only authoritative copy.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # upload_to ignores the client-supplied filename entirely (REQ-05);
    # the path is derived from `id` alone. See artifacts/storage.py.
    file = models.FileField(
        db_column='path', upload_to=storage.upload_to, storage=storage.artifact_storage, max_length=255,
    )

    # Verbatim, opaque, never parsed and never a path component (REQ-05).
    # blank=True so an upload needs nothing supplied alongside the file
    # (REQ-01); the admin fills it from the uploaded file when it is left
    # empty.
    original_filename = models.TextField(blank=True)

    # The authenticated admin user who uploaded. Artifacts are uploaded by
    # humans, never by agents (PRD §3).
    uploaded_by = models.TextField()

    created_at = models.DateTimeField(default=timezone.now)
    # Moves on re-upload (REQ-04); untouched by an in-place edit on the
    # volume, which the platform does not observe (REQ-06).
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'artifact'
        indexes = [
            models.Index(fields=['created_at'], name='idx_artifact_created'),
        ]

    @property
    def path(self) -> str:
        """The recorded storage-relative path."""
        return self.file.name

    def __str__(self) -> str:
        return f'{self.id} ({self.original_filename or "unnamed"})'


class ArtifactAccessLog(models.Model):
    """Retrieval is a read, and internal-work-item-service.md REQ-04
    requires read access to be "recorded in the service's own API/access
    logs, not in Streams".

    A table of this app's own rather than a row in ``workitems``'
    ``access_log``: that table's subject column is ``work_item_id``, and an
    artifact id is not a work-item id. Same shape, same purpose, truthful
    column. (Generalizing ``access_log``'s subject column so both can share
    it is proposed to the track that owns ``workitems/``.)
    """

    id = models.BigAutoField(primary_key=True)
    occurred_at = models.DateTimeField(default=timezone.now)
    operation = models.TextField()
    # Nullable: a read of an id that resolves to nothing is still a read,
    # and is still recorded.
    artifact_id = models.UUIDField(null=True, blank=True)
    actor = models.TextField(null=True, blank=True)

    class Meta:
        db_table = 'artifact_access_log'
        indexes = [
            models.Index(fields=['occurred_at'], name='idx_artifact_access_occurred'),
        ]

    def __str__(self) -> str:
        return f'{self.operation} @ {self.occurred_at}'
