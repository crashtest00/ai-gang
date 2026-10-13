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
to the single ``data`` field and the ``msg-<uuid>`` message id. ``kind`` is
``Kind.ARTIFACT_EVENT``, in that module's ``VALID_KINDS``, so a consumer
using its ``from_stream_fields`` reads these entries like any other.

**Durability** (durable-artifact-event-history.md REQ-01, REQ-03). The
envelope is not published here. ``record_upload`` writes it to Core's
transactional outbox (``workitems.store.write_outbox_event``) inside the
same transaction as the ``artifact`` row it announces, naming this stream
as its destination; ``python manage.py relay`` publishes it verbatim once
Redis takes it. An upload whose transaction rolls back announces nothing,
and a committed upload is announced even if Redis is down when it commits.
The envelope is validated against ``workitems.envelope`` when the row is
written, so a malformed one cannot commit. Its ``messageId`` and
``createdAt`` are fixed then too, so a relay retry republishes the same
message, and the relay's per-row dedupe key (``outbox:<row id>``) keeps a
retry from adding a second entry. A re-upload of the same bytes to the
same path is still a second, legitimate event (``action`` is ``replaced``)
with its own row.

One field is unlike a work item's: ``project`` carries the fixed sentinel
``_instance``, because the envelope requires a non-empty project and an
artifact has none.
"""

from __future__ import annotations

from datetime import datetime, timezone as dt_timezone
from typing import Any

from workitems.envelope import SCHEMA_VERSION, Kind, new_message_id
from workitems.store import write_outbox_event

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


def record_upload(artifact, *, created: bool) -> str:
    """Write the upload event to the outbox and return the outbox row's id.

    Called from the admin's ``save_model`` inside the transaction that
    saves the ``artifact`` row, after the save has assigned the stored
    path. The row commits or rolls back with the upload; the relay
    publishes it to ``aigang:artifacts:events``.
    """
    return write_outbox_event(stream=ARTIFACT_EVENT_STREAM,
                              envelope=_build_upload_envelope(artifact, created=created))
