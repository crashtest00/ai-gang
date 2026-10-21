"""
The Streams subscriber — librarian.md REQ-01 and REQ-06.

"The librarian MUST subscribe to artifact-request messages on Redis
Streams", following the ack/retry/dead-letter pattern
``redis-streams.md`` establishes and ``workitems/streams.py`` already
implements. That module is imported whole and not subclassed: the
consumer group, the blocking ``XREADGROUP`` loop, the envelope decode,
``XACK``, the attempt counter, the reclaim of stale pending entries and
the dead-letter write are all its. The librarian's two kinds are in
``workitems.envelope.VALID_KINDS``, so the shared decode accepts a
delivery request exactly as it accepts a work-item command, and this app
contributes only the handler.

**Why a failure is answered rather than retried.** REQ-06 requires
*exactly one* answer to every request, and "no request completes without a
response". A handler that raised on a copy failure would be retried three
times and then dead-lettered, leaving the requester with no answer at all
— and a handler that answered *and* raised would answer up to three times.
So every request this consumer can decode is answered exactly once and
acked, including when the answer is a failure. The answer is committed to
Core's transactional outbox before the handler returns, and the relay
publishes it (``responses.py``); the one exception is a database that
cannot take even the failure answer, which leaves the request
unacknowledged to be retried and finally dead-lettered unanswered
(durable-artifact-event-history.md REQ-04). The requester decides
whether to ask again, which is PRD §8's "ask, don't push" applied to the
failure path as well as the happy one.

**What still dead-letters**, through ``workitems.streams``' own machinery:
an entry that is not an envelope at all, and one whose kind is not a
delivery request. Neither carries a requester this librarian could answer
— the first has no correlation id, and the second is somebody else's
message on the wrong stream — so the handler raises ``PermanentError``
for the second rather than minting a response nobody asked for, and both
land on ``aigang:librarian:requests:dead``.
"""

from __future__ import annotations

import os
from typing import Any

from workitems.streams import PermanentError, create_consumer

from . import responses
from .delivery import deliver, parse_request
from .envelope import REQUEST_KIND
from .failures import DeliveryFailure
from .stream_topology import REQUEST_GROUP, REQUEST_STREAM


def handle_request(entry_envelope: dict[str, Any]) -> None:
    """Handle one decoded request and commit its single answer to the
    outbox before returning — the consumer acknowledges the request only
    after this returns (``workitems.streams.Consumer``), so the answer is
    durable before the acknowledgement (durable-artifact-event-history.md
    REQ-04). A request that already has an answer gets no second one.

    Never raises for a request-shaped message while the database can take
    its answer: every outcome, including an unexpected one, becomes an
    answer (see the module docstring). When the database cannot take even
    the failure answer, that error propagates, the request stays
    unacknowledged, and ``workitems.streams`` retries it and finally
    dead-letters it unanswered.
    """
    payload = entry_envelope.get('payload') or {}
    correlation_id = entry_envelope.get('messageId')
    request = None
    try:
        request = parse_request(payload)
        deliver(request, correlation_id=correlation_id)
    except DeliveryFailure as raised:
        failure = raised
    except Exception as err:  # noqa: BLE001 - REQ-06: the requester is told, not left waiting
        failure = DeliveryFailure('copy_failed', f'the librarian could not complete this request: {err}')
    else:
        return
    responses.record_failure(failure, correlation_id=correlation_id, request=request, payload=payload)


def create_librarian_consumer(redis_factory, *, consumer_name: str | None = None):
    """The librarian's subscriber, wired the way ``manage.py
    run_librarian`` runs it. Its answers go to the outbox, not to Redis,
    so the handler needs no client of its own; ``python manage.py relay``
    publishes them."""

    def _handler(entry_envelope: dict[str, Any]) -> None:
        kind = entry_envelope.get('kind')
        if kind != REQUEST_KIND:
            # Dead-lettered without consuming a retry slot: a second
            # delivery would not make it a request either.
            raise PermanentError(f'not a request: kind={kind!r}')
        handle_request(entry_envelope)

    return create_consumer(
        redis_factory,
        stream=REQUEST_STREAM,
        group=REQUEST_GROUP,
        consumer_name=consumer_name or os.uname().nodename,
        handler=_handler,
    )
