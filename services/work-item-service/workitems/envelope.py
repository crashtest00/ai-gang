"""
Envelope schema — a Python port of services/scrummaster/src/envelope.js's wire
format, byte-for-byte compatible with it (same field names, same
schemaVersion, same JSON shape under the stream entry's single `data`
field). This is NOT a require()-style reuse (impossible cross-language);
Redis Streams is a wire protocol, not a language-specific library, so the
envelope is reimplemented here against that shared wire contract instead.

Kept deliberately narrow: only KIND.WORK_ITEM_COMMAND / WORK_ITEM_EVENT and
KIND.WEBHOOK_EVENT (the kind ScrumMaster's server.js already publishes Jira
webhooks as, and which webhook_consumer.py reads) are meaningful to this
service, but every kind is listed so an envelope produced by any other AI
Gang component still validates here if it ever needs to (mirrors
envelope.js's own VALID_KINDS covering kinds this service doesn't use).

V4's three kinds — artifact_event (artifacts/events.py) and the librarian's
request/response pair — are in that list for exactly this reason. They are
published from apps in this same instance, onto streams this module's own
publish path carries; a kind missing here does not make a stream private,
it only forces the app that owns it to bypass streams.publish's validation
and dedupe, which is what both of them had done.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = '1'


class Kind:
    TASK = 'task'
    TASK_STATUS = 'task_status'
    JIRA_OPERATION = 'jira_operation'
    WEBHOOK_EVENT = 'webhook_event'
    WORK_ITEM_COMMAND = 'work_item_command'
    WORK_ITEM_EVENT = 'work_item_event'
    # V4 — artifact custody. ARTIFACT_EVENT is artifacts/events.py's upload
    # announcement; the delivery pair is the librarian's request/response
    # contract (librarian/envelope.py, librarian/README.md).
    ARTIFACT_EVENT = 'artifact_event'
    ARTIFACT_DELIVERY_REQUEST = 'artifact_delivery_request'
    ARTIFACT_DELIVERY_RESPONSE = 'artifact_delivery_response'


VALID_KINDS = {
    Kind.TASK, Kind.TASK_STATUS, Kind.JIRA_OPERATION,
    Kind.WEBHOOK_EVENT, Kind.WORK_ITEM_COMMAND, Kind.WORK_ITEM_EVENT,
    Kind.ARTIFACT_EVENT, Kind.ARTIFACT_DELIVERY_REQUEST, Kind.ARTIFACT_DELIVERY_RESPONSE,
}

# Kinds that carry A2A task identity and therefore require taskId/contextId.
TASK_KINDS = {Kind.TASK, Kind.TASK_STATUS}


class EnvelopeError(ValueError):
    pass


def new_message_id() -> str:
    # Matches a2a/ids.js's "msg-<uuid>" convention (envelope.js's own
    # comment: "so the two features don't mint competing shapes").
    return f'msg-{uuid.uuid4()}'


def build_envelope(
    kind: str,
    project: str,
    *,
    task_id: str | None = None,
    context_id: str | None = None,
    correlation_id: str | None = None,
    payload: dict[str, Any] | None = None,
    message_id: str | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    envelope = {
        'schemaVersion': SCHEMA_VERSION,
        'messageId': message_id or new_message_id(),
        'kind': kind,
        'project': project,
        'taskId': task_id,
        'contextId': context_id,
        'correlationId': correlation_id,
        'createdAt': created_at or datetime.now(timezone.utc).isoformat(),
        'payload': payload or {},
    }
    validate_envelope(envelope)
    return envelope


def validate_envelope(envelope: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(envelope, dict):
        raise EnvelopeError('envelope must be an object')
    if envelope.get('schemaVersion') != SCHEMA_VERSION:
        raise EnvelopeError(f'envelope.schemaVersion must be "{SCHEMA_VERSION}", got {envelope.get("schemaVersion")!r}')
    message_id = envelope.get('messageId')
    if not isinstance(message_id, str) or len(message_id) == 0:
        raise EnvelopeError('envelope.messageId is required')
    kind = envelope.get('kind')
    if kind not in VALID_KINDS:
        raise EnvelopeError(f'envelope.kind must be one of {sorted(VALID_KINDS)}, got {kind!r}')
    project = envelope.get('project')
    if not isinstance(project, str) or len(project) == 0:
        raise EnvelopeError('envelope.project is required')
    if kind in TASK_KINDS:
        if not isinstance(envelope.get('taskId'), str) or len(envelope['taskId']) == 0:
            raise EnvelopeError(f'envelope.taskId is required for kind={kind}')
        if not isinstance(envelope.get('contextId'), str) or len(envelope['contextId']) == 0:
            raise EnvelopeError(f'envelope.contextId is required for kind={kind}')
    created_at = envelope.get('createdAt')
    if not isinstance(created_at, str) or not _parses_as_timestamp(created_at):
        raise EnvelopeError('envelope.createdAt must be an RFC 3339 timestamp string')
    payload = envelope.get('payload')
    if payload is None or not isinstance(payload, dict):
        raise EnvelopeError('envelope.payload must be an object')
    return envelope


def _parses_as_timestamp(value: str) -> bool:
    try:
        datetime.fromisoformat(value.replace('Z', '+00:00'))
        return True
    except ValueError:
        return False


def to_stream_fields(envelope: dict[str, Any]) -> dict[str, str]:
    import json
    return {'data': json.dumps(envelope)}


def from_stream_fields(fields: dict[str, Any] | None):
    import json
    try:
        raw = fields.get('data') if fields else None
        if not isinstance(raw, str):
            return None
        envelope = json.loads(raw)
        validate_envelope(envelope)
        return envelope
    except Exception:
        return None
