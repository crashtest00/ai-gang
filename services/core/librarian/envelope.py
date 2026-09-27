"""
The librarian's wire envelope — librarian.md REQ-01, open question 1.

The shape is ``redis-streams.md``'s envelope exactly: this module holds no
copy of it. ``workitems.envelope`` defines the schema version, the
``msg-<uuid>`` message id, the single ``data`` field a stream entry
carries, the kind vocabulary — which now includes this app's two kinds,
``Kind.ARTIFACT_DELIVERY_REQUEST`` and ``Kind.ARTIFACT_DELIVERY_RESPONSE``
— and the validation every envelope on this platform is held to. All of
that is imported, so the two can never drift, and the librarian's
subscriber is ``workitems.streams``' own consumer with no envelope
override (``consumer.py``).

What is left here is the two things that are this app's and not that
module's: the ``_instance`` project sentinel these instance-wide streams
carry, and the field spelling below.

**Field spelling.** REQ-01 names the request's fields ``requested_by``,
``artifact_id``, ``destination_repo``, ``requested_path`` and ``task_id``.
Every other payload on this platform is camelCase (``workitems``' commands,
``artifacts``' events), so the librarian publishes camelCase and accepts
either spelling on the way in — ``read_field`` below is the one place that
is decided. A requester written straight from REQ-01's words works, and so
does one written to the convention.
"""

from __future__ import annotations

from typing import Any, Optional

from workitems.envelope import Kind, to_stream_fields
from workitems.envelope import build_envelope as build_platform_envelope

# The envelope's `project` is required and non-empty; a delivery request
# names a repository, not a project, and the streams are instance-wide.
# Same sentinel, for the same reason, as artifacts/events.py's.
INSTANCE_SCOPE = '_instance'

REQUEST_KIND = Kind.ARTIFACT_DELIVERY_REQUEST
RESPONSE_KIND = Kind.ARTIFACT_DELIVERY_RESPONSE


def build_envelope(kind: str, *, payload: dict[str, Any], correlation_id: Optional[str] = None,
                   task_id: Optional[str] = None, message_id: Optional[str] = None) -> dict[str, Any]:
    """``workitems.envelope.build_envelope`` with this app's project
    sentinel filled in — and its validation, which is what refuses a kind
    that is not one of the platform's."""
    return build_platform_envelope(
        kind, INSTANCE_SCOPE,
        task_id=task_id, correlation_id=correlation_id, payload=payload, message_id=message_id,
    )


def read_field(payload: dict[str, Any], camel: str, snake: str) -> Any:
    """Read one request field under either spelling — see the module
    docstring. camelCase wins if both are present, which only happens in a
    request that contradicts itself."""
    value = payload.get(camel)
    if value is None:
        value = payload.get(snake)
    return value


__all__ = [
    'INSTANCE_SCOPE', 'REQUEST_KIND', 'RESPONSE_KIND',
    'build_envelope', 'read_field', 'to_stream_fields',
]
