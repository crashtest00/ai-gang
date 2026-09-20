"""
The librarian's wire envelope — librarian.md REQ-01, open question 1.

The shape is ``redis-streams.md``'s envelope exactly, and the constants
that define it (``SCHEMA_VERSION``, the ``msg-<uuid>`` message id, the
single ``data`` field a stream entry carries) are imported from
``workitems.envelope`` rather than restated, so the two can never drift.

**Why this module exists at all.** ``workitems.envelope.VALID_KINDS`` is a
closed set of work-item and Jira kinds; ``artifact_delivery_request`` is
not in it, so ``workitems.envelope.validate_envelope`` would reject every
message on this app's streams. Adding the two kinds there is a two-line
change in a package this track does not own, so it is raised as a proposal
and this module carries its own kind set in the meantime. Everything else
about the envelope is that module's, imported.

**Field spelling.** REQ-01 names the request's fields ``requested_by``,
``artifact_id``, ``destination_repo``, ``requested_path`` and ``task_id``.
Every other payload on this platform is camelCase (``workitems``' commands,
``artifacts``' events), so the librarian publishes camelCase and accepts
either spelling on the way in — ``read_field`` below is the one place that
is decided. A requester written straight from REQ-01's words works, and so
does one written to the convention.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from workitems.envelope import SCHEMA_VERSION, new_message_id, to_stream_fields

# The envelope's `project` is required and non-empty; a delivery request
# names a repository, not a project, and the streams are instance-wide.
# Same sentinel, for the same reason, as artifacts/events.py's.
INSTANCE_SCOPE = '_instance'

REQUEST_KIND = 'artifact_delivery_request'
RESPONSE_KIND = 'artifact_delivery_response'

VALID_KINDS = {REQUEST_KIND, RESPONSE_KIND}


def build_envelope(kind: str, *, payload: dict[str, Any], correlation_id: Optional[str] = None,
                   task_id: Optional[str] = None, message_id: Optional[str] = None) -> dict[str, Any]:
    """An envelope of this app's own kinds, otherwise identical to what
    ``workitems.envelope.build_envelope`` produces."""
    if kind not in VALID_KINDS:
        raise ValueError(f'kind must be one of {sorted(VALID_KINDS)}, got {kind!r}')
    return {
        'schemaVersion': SCHEMA_VERSION,
        'messageId': message_id or new_message_id(),
        'kind': kind,
        'project': INSTANCE_SCOPE,
        'taskId': task_id,
        'contextId': None,
        'correlationId': correlation_id,
        'createdAt': datetime.now(timezone.utc).isoformat(),
        'payload': payload,
    }


def validate_envelope(envelope: Any) -> dict[str, Any]:
    """The same checks ``workitems.envelope.validate_envelope`` makes, over
    this module's kinds. Raises ``ValueError`` on anything malformed."""
    if not isinstance(envelope, dict):
        raise ValueError('envelope must be an object')
    if envelope.get('schemaVersion') != SCHEMA_VERSION:
        raise ValueError(f'envelope.schemaVersion must be "{SCHEMA_VERSION}"')
    if not isinstance(envelope.get('messageId'), str) or not envelope['messageId']:
        raise ValueError('envelope.messageId is required')
    if envelope.get('kind') not in VALID_KINDS:
        raise ValueError(f'envelope.kind must be one of {sorted(VALID_KINDS)}, got {envelope.get("kind")!r}')
    if not isinstance(envelope.get('project'), str) or not envelope['project']:
        raise ValueError('envelope.project is required')
    if not isinstance(envelope.get('payload'), dict):
        raise ValueError('envelope.payload must be an object')
    return envelope


def from_stream_fields(fields: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Decode a stream entry into an envelope, or ``None`` if it is not one
    — the same contract, and the same silent-``None`` convention, as
    ``workitems.envelope.from_stream_fields``. ``None`` is what sends an
    entry to the dead-letter stream (see ``consumer.py``): a message this
    malformed carries no correlation id and no requester, so there is
    nobody to answer."""
    try:
        raw = fields.get('data') if fields else None
        if isinstance(raw, bytes):
            raw = raw.decode()
        if not isinstance(raw, str):
            return None
        return validate_envelope(json.loads(raw))
    except Exception:  # noqa: BLE001 - mirrors workitems.envelope's own catch-all
        return None


def read_field(payload: dict[str, Any], camel: str, snake: str) -> Any:
    """Read one request field under either spelling — see the module
    docstring. camelCase wins if both are present, which only happens in a
    request that contradicts itself."""
    value = payload.get(camel)
    if value is None:
        value = payload.get(snake)
    return value


__all__ = [
    'INSTANCE_SCOPE', 'REQUEST_KIND', 'RESPONSE_KIND', 'VALID_KINDS',
    'build_envelope', 'validate_envelope', 'from_stream_fields', 'read_field',
    'to_stream_fields',
]
