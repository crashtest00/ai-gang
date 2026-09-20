"""
The upload event — artifact-ingress.md REQ-06.

One Redis Streams entry per successful upload and per re-upload, carrying
the canonical id, the stored path, whether the upload created or replaced
the artifact, the actor and the timestamp. An in-place edit on the volume
publishes nothing: "the stream records arrivals, not every change"
(REQ-06).

**Namespace.** Artifacts are not a work-item feature and are not
per-project — an upload names no project — so this does not publish under
``aigang:workitems:{project}:...``. The stream is instance-wide:

    aigang:artifacts:events

matching `redis-streams.md`'s ``aigang:<domain>[:{project}]`` convention
with the project segment absent because there is no project. As with
``workitems``' own event stream, no fixed consumer group is created here;
an interested subscriber creates its own.

**Envelope.** The same wire shape ``workitems/envelope.py`` defines, down
to the single ``data`` field and the ``msg-<uuid>`` message id, and
published through ``workitems.streams.publish`` — the one publish path
this service has, which validates the envelope before it writes and
carries the SET-NX dedupe. ``kind`` is ``Kind.ARTIFACT_EVENT``, in that
module's ``VALID_KINDS``, so a consumer using its ``from_stream_fields``
reads these entries like any other.

One field is unlike a work item's: ``project`` carries the fixed sentinel
``_instance``, because the envelope requires a non-empty project and an
artifact has none.
"""

from __future__ import annotations

from datetime import datetime, timezone as dt_timezone
from typing import Any, Optional

from workitems.envelope import SCHEMA_VERSION, Kind, new_message_id
from workitems.redis_client import get_client
from workitems.streams import publish

ARTIFACT_EVENT_STREAM = 'aigang:artifacts:events'
ARTIFACT_EVENT_KIND = Kind.ARTIFACT_EVENT
ARTIFACT_UPLOADED = 'artifact.uploaded'

# The envelope's `project` field is required and an artifact belongs to no
# project; this marks the event as instance-wide rather than inventing one.
INSTANCE_SCOPE = '_instance'

ACTION_CREATED = 'created'
ACTION_REPLACED = 'replaced'


def _build_upload_envelope(artifact, *, created: bool) -> dict[str, Any]:
    uploaded_at = artifact.updated_at or datetime.now(dt_timezone.utc)
    return {
        'schemaVersion': SCHEMA_VERSION,
        'messageId': new_message_id(),
        'kind': ARTIFACT_EVENT_KIND,
        'project': INSTANCE_SCOPE,
        'taskId': None,
        'contextId': None,
        'correlationId': None,
        'createdAt': uploaded_at.isoformat(),
        'payload': {
            'eventType': ARTIFACT_UPLOADED,
            'artifactId': str(artifact.id),
            'path': artifact.path,
            'action': ACTION_CREATED if created else ACTION_REPLACED,
            'actor': artifact.uploaded_by,
            'uploadedAt': uploaded_at.isoformat(),
        },
    }


def publish_upload(artifact, *, created: bool, client: Optional[Any] = None) -> str:
    """Publish the upload event and return its stream entry id.

    Called from the admin's ``save_model`` through
    ``transaction.on_commit``, so nothing is announced for an upload whose
    record did not commit. The converse gap — a commit whose publish then
    fails — is real: the caller catches and logs it (``artifacts/admin.py``)
    so the committed upload still answers success, and the transactional
    outbox ``workitems`` uses for that guarantee is proposed, not built,
    because this spec does not require it.

    ``workitems.streams.publish`` rather than a bare ``XADD``: it is this
    service's one publish path, and it validates the envelope against
    ``workitems.envelope`` before anything reaches the stream, so a
    malformed event cannot be written here and rejected by every reader.
    """
    client = client or get_client()
    result = publish(client, ARTIFACT_EVENT_STREAM, _build_upload_envelope(artifact, created=created))
    return result['entryId']
