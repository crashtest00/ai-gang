"""
The read path for artifacts — artifact-ingress.md REQ-03, following
``workitems/readstore.py``'s pattern for internal-work-item-service.md
REQ-04: a direct synchronous read with no state-changing effect, recorded
in the service's own access log rather than on Streams.
"""

from __future__ import annotations

from typing import Optional

from django.utils import timezone

from .models import Artifact, ArtifactAccessLog


def _log_access(operation: str, *, artifact_id=None, actor: Optional[str] = None) -> None:
    ArtifactAccessLog.objects.create(
        operation=operation, artifact_id=artifact_id, actor=actor, occurred_at=timezone.now(),
    )


def get_artifact(artifact_id, *, actor: Optional[str] = None) -> Optional[Artifact]:
    """Fetch an artifact record by canonical id, logging the access.

    Returns the record, not the bytes: REQ-02 means the bytes are read
    from the file on the volume at the moment of the request, so nothing
    here holds them.
    """
    artifact = Artifact.objects.filter(id=artifact_id).first()
    _log_access('getArtifact', artifact_id=artifact_id, actor=actor)
    return artifact
