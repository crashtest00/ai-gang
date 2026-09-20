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
to the single ``data`` field and the ``msg-<uuid>`` message id, which is
why ``to_stream_fields``/``new_message_id`` are imported from there rather
than reimplemented. Two fields differ, both because that envelope was
written for work items:

- ``kind`` is ``artifact_event``, which is NOT in
  ``workitems.envelope.VALID_KINDS`` — so a consumer using that module's
  ``from_stream_fields`` would reject these entries. Adding the kind there
  is a one-line change in a package this track does not own; it is raised
  as a proposal rather than made here. No consumer of this stream exists
  yet.
- ``project`` carries the fixed sentinel ``_instance`` because the
  envelope requires a non-empty project and an artifact has none.
"""

from __future__ import annotations

from datetime import datetime, timezone as dt_timezone
from typing import Any, Optional

from workitems.envelope import SCHEMA_VERSION, new_message_id, to_stream_fields
from workitems.redis_client import get_client

ARTIFACT_EVENT_STREAM = 'aigang:artifacts:events'
ARTIFACT_EVENT_KIND = 'artifact_event'
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
    fails — is real and is reported rather than papered over; the
    transactional outbox ``workitems`` uses for that guarantee is proposed,
    not built, because this spec does not require it.
    """
    client = client or get_client()
    entry_id = client.xadd(ARTIFACT_EVENT_STREAM, to_stream_fields(_build_upload_envelope(artifact, created=created)))
    return entry_id.decode() if isinstance(entry_id, bytes) else entry_id
