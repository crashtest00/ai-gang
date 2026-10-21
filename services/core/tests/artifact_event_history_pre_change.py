"""
The pre-change publishers, frozen — durable-artifact-event-history.md
REQ-02, REQ-03 and REQ-07 compare what the outbox now publishes against
what the code published before it.

Copied verbatim from AI Gang Source ``6077676`` (the specification's pin),
with the names changed so they cannot be mistaken for production code and
the module constants they read written out as their values:

- ``relay_envelope``: ``workitems/relay.py``'s ``relay_once`` loop body,
  lines 51-69 — the envelope and stream the relay built for every row.
- ``publish_upload``: ``artifacts/events.py``'s ``_build_upload_envelope``
  and ``publish_upload``.
- ``handle_request``: ``librarian/consumer.py``'s ``handle_request``, with
  ``librarian/delivery.py``'s ``deliver`` and ``librarian/responses.py``'s
  ``_publish``, ``publish_confirmation`` and ``publish_failure`` — the
  pre-change librarian publishes its answer straight to Redis. The
  resolution helpers it calls (``_artifact_or_failure``, ``paths``,
  ``_source_file``, ``_lock``, ``_test_only_hold_delay``, ``_resolve``) are
  unchanged by this feature and are imported from ``librarian.delivery``.

Not collected by pytest (``pytest.ini``'s ``python_files = test_*.py``).
Nothing outside the tests imports it.
"""

from __future__ import annotations

from datetime import datetime, timezone as dt_timezone
from typing import Any, Optional

from django.db import transaction

from workitems.envelope import SCHEMA_VERSION, Kind, new_message_id, validate_envelope
from workitems.stream_topology import event_stream_name
from workitems.streams import publish

# --- workitems/relay.py at 6077676 ----------------------------------------


def relay_envelope(row) -> tuple[str, dict[str, Any]]:
    envelope = {
        'schemaVersion': '1',
        'messageId': str(row.id),  # deterministic: stable across retries of the SAME outbox row.
        'kind': Kind.WORK_ITEM_EVENT,
        'project': row.project,
        'taskId': None,
        'contextId': None,
        'correlationId': None,
        'createdAt': row.created_at.isoformat(),
        'payload': {
            'outboxId': str(row.id),
            'eventType': row.event_type,
            'workItemId': str(row.work_item_id) if row.work_item_id else None,
            'data': row.payload,
        },
    }
    validate_envelope(envelope)

    stream = event_stream_name(row.project)
    return stream, envelope


# --- artifacts/events.py at 6077676 ---------------------------------------

ARTIFACT_EVENT_STREAM = 'aigang:artifacts:events'


def _build_upload_envelope(artifact, *, created: bool) -> dict[str, Any]:
    uploaded_at = artifact.updated_at or datetime.now(dt_timezone.utc)
    return {
        'schemaVersion': SCHEMA_VERSION,
        'messageId': new_message_id(),
        'kind': Kind.ARTIFACT_EVENT,
        'project': '_instance',
        'taskId': None,
        'contextId': None,
        'correlationId': None,
        'createdAt': uploaded_at.isoformat(),
        'payload': {
            'eventType': 'artifact.uploaded',
            'artifactId': str(artifact.id),
            'path': artifact.path,
            'action': 'created' if created else 'replaced',
            'actor': artifact.uploaded_by,
            'uploadedAt': uploaded_at.isoformat(),
        },
    }


def publish_upload(artifact, *, created: bool, client) -> str:
    result = publish(client, ARTIFACT_EVENT_STREAM, _build_upload_envelope(artifact, created=created))
    return result['entryId']


# --- librarian/responses.py, delivery.py and consumer.py at 6077676 --------

def _publish(client, payload: dict[str, Any], *, correlation_id: Optional[str],
             task_id: Optional[str]) -> str:
    from librarian.envelope import RESPONSE_KIND, build_envelope
    from librarian.stream_topology import RESPONSE_STREAM

    envelope = build_envelope(RESPONSE_KIND, payload=payload, correlation_id=correlation_id, task_id=task_id)
    result = publish(client, RESPONSE_STREAM, envelope)
    return result['entryId']


def publish_confirmation(client, request, confirmation: dict[str, Any], *,
                         correlation_id: Optional[str]) -> str:
    return _publish(client, {
        'status': 'delivered',
        'artifactId': request.artifact_id,
        'destinationRepo': request.destination_repo,
        'requestedBy': request.requested_by,
        'taskId': request.task_id,
        **confirmation,
    }, correlation_id=correlation_id, task_id=request.task_id)


def publish_failure(client, failure, *, correlation_id: Optional[str], request=None,
                    payload: Optional[dict[str, Any]] = None) -> str:
    from librarian.envelope import read_field

    payload = payload or {}
    return _publish(client, {
        'status': 'failed',
        'reason': failure.reason,
        'detail': failure.detail,
        'artifactId': request.artifact_id if request else read_field(payload, 'artifactId', 'artifact_id'),
        'destinationRepo': (request.destination_repo if request
                            else read_field(payload, 'destinationRepo', 'destination_repo')),
        'requestedBy': request.requested_by if request else read_field(payload, 'requestedBy', 'requested_by'),
        'taskId': request.task_id if request else read_field(payload, 'taskId', 'task_id'),
    }, correlation_id=correlation_id, task_id=request.task_id if request else None)


def deliver(request) -> dict[str, Any]:
    from librarian import delivery, paths

    artifact = delivery._artifact_or_failure(request.artifact_id)
    repository = paths.resolve_repository(request.destination_repo)
    requested = paths.resolve_requested_path(repository, request.requested_path)
    source = delivery._source_file(artifact)

    with transaction.atomic():
        delivery._lock(str(artifact.id), repository.name)
        delivery._test_only_hold_delay()
        return delivery._resolve(request, artifact, repository, requested, source)


def handle_request(client, entry_envelope: dict[str, Any]) -> str:
    from librarian.delivery import parse_request
    from librarian.failures import DeliveryFailure

    payload = entry_envelope.get('payload') or {}
    correlation_id = entry_envelope.get('messageId')
    request = None
    try:
        request = parse_request(payload)
        confirmation = deliver(request)
    except DeliveryFailure as failure:
        return publish_failure(client, failure, correlation_id=correlation_id,
                               request=request, payload=payload)
    except Exception as err:  # noqa: BLE001 - REQ-06: the requester is told, not left waiting
        failure = DeliveryFailure('copy_failed', f'the librarian could not complete this request: {err}')
        return publish_failure(client, failure, correlation_id=correlation_id,
                               request=request, payload=payload)
    return publish_confirmation(client, request, confirmation, correlation_id=correlation_id)
