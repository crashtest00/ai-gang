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

**Durable, and exactly one** (durable-artifact-event-history.md REQ-04).
An answer is not published here. It is written to Core's transactional
outbox (``workitems.store.write_outbox_event``), naming the response stream
as its destination, and ``python manage.py relay`` publishes it verbatim
once Redis takes it. The consumer acknowledges the request only after that
write has committed (the handler returns, then ``workitems.streams``
acknowledges), so an answer survives a Redis outage and a crash between
the commit and the acknowledgement.

A confirmation's row is written inside ``delivery.deliver``'s transaction,
with the ``artifact_delivery`` record it confirms; a failure's row in its
own transaction (``record_failure``). Every transaction that may write an
answer first takes ``lock_answer`` — a transaction-scoped advisory lock on
the request's ``messageId`` — and then asks ``already_answered``; if the
request already has an answer row, confirmation or failure, it writes
nothing and the request is acknowledged with no new answer. Two handlings
of one request (a reclaim while the first is still running, or a redelivery
after an acknowledgement that failed) therefore serialize, and whichever
answer commits first is the one answer. The partial unique index
``uniq_outbox_librarian_answer`` (``workitems/models.py``, migration
``0008``) refuses a second answer row for one ``correlationId`` even if the
lookup were bypassed; ``is_answer_refusal`` recognises that refusal so the
caller treats it as already answered.

``librarian/README.md`` states the full contract, including the read
sequence an agent-facing helper follows.
"""

from __future__ import annotations

import hashlib
from typing import Any, Optional

from django.db import IntegrityError, connection, transaction
from django.db.models.fields.json import KeyTextTransform

from workitems.models import OutboxEvent
from workitems.store import write_outbox_event

from .envelope import RESPONSE_KIND, build_envelope, read_field
from .stream_topology import RESPONSE_STREAM

STATUS_DELIVERED = 'delivered'
STATUS_FAILED = 'failed'

# The partial unique index on the outbox's stored envelope (see the module
# docstring); its name is how a refused insert is told apart from any other
# integrity error.
ANSWER_INDEX = 'uniq_outbox_librarian_answer'


def answer_lock_key(correlation_id: str) -> int:
    """The Postgres advisory-lock key for one request's answer, keyed on
    the request envelope's ``messageId``. Prefixed so it cannot coincide
    with ``delivery.advisory_lock_key``'s (artifact, repository) keys."""
    digest = hashlib.sha256(f'librarian-answer\x00{correlation_id}'.encode()).digest()
    return int.from_bytes(digest[:8], 'big', signed=True)


def lock_answer(correlation_id: str) -> None:
    """``pg_advisory_xact_lock`` on the request's answer, held until the
    calling transaction commits or rolls back. Must be called inside
    ``transaction.atomic()``, before ``already_answered`` and before any
    write. ``delivery.deliver`` takes it after its (artifact, repository)
    lock; ``record_failure`` takes no other lock — so the two locks are
    always acquired in one order."""
    with connection.cursor() as cursor:
        cursor.execute('SELECT pg_advisory_xact_lock(%s)', [answer_lock_key(correlation_id)])


def already_answered(correlation_id: str) -> bool:
    """Whether an answer row — confirmation or failure — exists for this
    request: an outbox row destined for the response stream whose stored
    envelope's ``correlationId`` is the request's ``messageId``."""
    return (OutboxEvent.objects
            .annotate(answer_to=KeyTextTransform('correlationId', 'envelope'))
            .filter(stream=RESPONSE_STREAM, answer_to=correlation_id)
            .exists())


def is_answer_refusal(err: IntegrityError) -> bool:
    """Whether ``err`` is ``uniq_outbox_librarian_answer`` refusing a second
    answer for one request — which means the request is already answered."""
    diag = getattr(getattr(err, '__cause__', None), 'diag', None)
    return getattr(diag, 'constraint_name', None) == ANSWER_INDEX


def _record(payload: dict[str, Any], *, correlation_id: Optional[str], task_id: Optional[str]) -> str:
    """Write one answer to the outbox, in the caller's transaction, and
    return the outbox row's id. The envelope is built and validated here,
    against the shared envelope contract, exactly as every other stream
    write on this platform is (V4 audit Pass 2 row 28); the relay publishes
    it to ``aigang:librarian:responses`` unchanged."""
    envelope = build_envelope(RESPONSE_KIND, payload=payload, correlation_id=correlation_id, task_id=task_id)
    return write_outbox_event(stream=RESPONSE_STREAM, envelope=envelope)


def record_confirmation(request, confirmation: dict[str, Any], *, correlation_id: Optional[str]) -> str:
    """The success answer, written inside ``delivery.deliver``'s
    transaction so it commits with the ``artifact_delivery`` record it
    confirms. ``confirmation`` is what the resolution returned: the path it
    read back, what it did, and when the artifact reached this
    repository."""
    return _record({
        'status': STATUS_DELIVERED,
        'artifactId': request.artifact_id,
        'destinationRepo': request.destination_repo,
        'requestedBy': request.requested_by,
        'taskId': request.task_id,
        **confirmation,
    }, correlation_id=correlation_id, task_id=request.task_id)


def record_failure(failure, *, correlation_id: Optional[str], request=None,
                   payload: Optional[dict[str, Any]] = None) -> Optional[str]:
    """The failure answer, naming its reason (``failures.py``) and the
    sentence behind it, committed in its own transaction. Returns the
    outbox row's id, or ``None`` when the request already had an answer
    and nothing was written.

    A failure answers a request that wrote no state, so there is nothing
    to be atomic with. A database that cannot take the write raises out of
    here, and the request stays unacknowledged for ``workitems.streams`` to
    retry and finally dead-letter: an answer the database never recorded
    cannot be the durable one.

    ``request`` is ``None`` when the request could not be parsed far enough
    to build one — a missing field. The echoed fields then come straight
    from the raw payload, so even a malformed request's answer says which
    artifact and repository it was about where the requester supplied them.
    """
    payload = payload or {}
    try:
        with transaction.atomic():
            lock_answer(correlation_id)
            if already_answered(correlation_id):
                return None
            return _record({
                'status': STATUS_FAILED,
                'reason': failure.reason,
                'detail': failure.detail,
                'artifactId': request.artifact_id if request else read_field(payload, 'artifactId', 'artifact_id'),
                'destinationRepo': (request.destination_repo if request
                                    else read_field(payload, 'destinationRepo', 'destination_repo')),
                'requestedBy': (request.requested_by if request
                                else read_field(payload, 'requestedBy', 'requested_by')),
                'taskId': request.task_id if request else read_field(payload, 'taskId', 'task_id'),
            }, correlation_id=correlation_id, task_id=request.task_id if request else None)
    except IntegrityError as err:
        if is_answer_refusal(err):
            return None
        raise
