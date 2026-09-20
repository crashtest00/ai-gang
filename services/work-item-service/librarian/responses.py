"""
The one answer every request gets — librarian.md REQ-06.

"Each request MUST receive one response: a confirmation with the delivered
path, or a failure naming its reason ... and no request completes without a
response."

Responses go to ``aigang:librarian:responses`` (``stream_topology.py``),
with no consumer group: a response has exactly one interested reader, the
requester that is blocked waiting for it, and a group would hand it to
whichever member read first. The requester finds its own answer by
``correlationId``, which carries the request envelope's ``messageId`` —
a value the requester minted itself, so it knows what to look for before
it publishes.

``librarian/README.md`` states the full contract, including the read
sequence an agent-facing helper follows.
"""

from __future__ import annotations

from typing import Any, Optional

from .envelope import RESPONSE_KIND, build_envelope, read_field, to_stream_fields
from .stream_topology import RESPONSE_STREAM

STATUS_DELIVERED = 'delivered'
STATUS_FAILED = 'failed'


def _publish(client, payload: dict[str, Any], *, correlation_id: Optional[str],
             task_id: Optional[str]) -> str:
    envelope = build_envelope(RESPONSE_KIND, payload=payload, correlation_id=correlation_id, task_id=task_id)
    entry_id = client.xadd(RESPONSE_STREAM, to_stream_fields(envelope))
    return entry_id.decode() if isinstance(entry_id, bytes) else entry_id


def publish_confirmation(client, request, confirmation: dict[str, Any], *,
                         correlation_id: Optional[str]) -> str:
    """The success answer. ``confirmation`` is what ``delivery.deliver``
    returned: the path it read back, what it did, and when the artifact
    reached this repository."""
    return _publish(client, {
        'status': STATUS_DELIVERED,
        'artifactId': request.artifact_id,
        'destinationRepo': request.destination_repo,
        'requestedBy': request.requested_by,
        'taskId': request.task_id,
        **confirmation,
    }, correlation_id=correlation_id, task_id=request.task_id)


def publish_failure(client, failure, *, correlation_id: Optional[str], request=None,
                    payload: Optional[dict[str, Any]] = None) -> str:
    """The failure answer, naming its reason (``failures.py``) and the
    sentence behind it.

    ``request`` is ``None`` when the request could not be parsed far enough
    to build one — a missing field. The echoed fields then come straight
    from the raw payload, so even a malformed request's answer says which
    artifact and repository it was about where the requester supplied them.
    """
    payload = payload or {}
    return _publish(client, {
        'status': STATUS_FAILED,
        'reason': failure.reason,
        'detail': failure.detail,
        'artifactId': request.artifact_id if request else read_field(payload, 'artifactId', 'artifact_id'),
        'destinationRepo': (request.destination_repo if request
                            else read_field(payload, 'destinationRepo', 'destination_repo')),
        'requestedBy': request.requested_by if request else read_field(payload, 'requestedBy', 'requested_by'),
        'taskId': request.task_id if request else read_field(payload, 'taskId', 'task_id'),
    }, correlation_id=correlation_id, task_id=request.task_id if request else None)
