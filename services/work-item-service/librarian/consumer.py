"""
The Streams subscriber — librarian.md REQ-01 and REQ-06.

"The librarian MUST subscribe to artifact-request messages on Redis
Streams", following the ack/retry/dead-letter pattern
``redis-streams.md`` establishes and ``workitems/streams.py`` already
implements. That module is imported, not copied: the consumer group, the
blocking ``XREADGROUP`` loop, ``XACK``, the attempt counter, the reclaim
of stale pending entries and the dead-letter write are all its.

**The one override.** ``workitems.streams.Consumer`` decodes each entry
with ``workitems.envelope.from_stream_fields``, whose ``VALID_KINDS`` does
not contain ``artifact_delivery_request`` — every librarian request would
be dead-lettered as an invalid envelope. ``_process_entry`` is therefore
overridden to decode with this app's envelope instead. It is the only
difference, and it disappears the day the two kinds are added to
``workitems/envelope.py`` (proposed, not done: that package belongs to
another track).

**Why a failure is answered rather than retried.** REQ-06 requires
*exactly one* answer to every request, and "no request completes without a
response". A handler that raised on a copy failure would be retried three
times and then dead-lettered, leaving the requester with no answer at all
— and a handler that answered *and* raised would answer up to three times.
So every request this consumer can decode is answered exactly once and
acked, including when the answer is a failure. The requester decides
whether to ask again, which is PRD §8's "ask, don't push" applied to the
failure path as well as the happy one. The retry and dead-letter machinery
is still live for the case it fits: an entry that is not a librarian
envelope at all carries no correlation id and no requester, so there is
nobody to answer, and it goes to ``aigang:librarian:requests:dead``.
"""

from __future__ import annotations

import os
from typing import Any

from workitems.streams import Consumer, attempts_key, dead_letter, record_success

from . import envelope as librarian_envelope
from . import responses
from .delivery import deliver, parse_request
from .failures import DeliveryFailure
from .stream_topology import REQUEST_GROUP, REQUEST_STREAM


def handle_request(client, entry_envelope: dict[str, Any]) -> str:
    """Handle one decoded request and publish its single response. Returns
    the response's stream entry id.

    Never raises for a request-shaped message: every outcome, including an
    unexpected one, becomes a response (see the module docstring).
    """
    payload = entry_envelope.get('payload') or {}
    correlation_id = entry_envelope.get('messageId')
    request = None
    try:
        request = parse_request(payload)
        confirmation = deliver(request)
    except DeliveryFailure as failure:
        return responses.publish_failure(client, failure, correlation_id=correlation_id,
                                         request=request, payload=payload)
    except Exception as err:  # noqa: BLE001 - REQ-06: the requester is told, not left waiting
        failure = DeliveryFailure('copy_failed', f'the librarian could not complete this request: {err}')
        return responses.publish_failure(client, failure, correlation_id=correlation_id,
                                         request=request, payload=payload)
    return responses.publish_confirmation(client, request, confirmation, correlation_id=correlation_id)


class LibrarianConsumer(Consumer):
    """``workitems.streams.Consumer`` with this app's envelope. See the
    module docstring for why the override exists and when it goes away."""

    def _process_entry(self, entry_id: str, fields: dict) -> None:
        entry_envelope = librarian_envelope.from_stream_fields(fields)
        if entry_envelope is None:
            dead_letter(self._client, self.stream, self.group, entry_id, {'raw': fields}, 'invalid_envelope',
                        self._get_attempts(entry_id))
            return
        if entry_envelope.get('kind') != librarian_envelope.REQUEST_KIND:
            dead_letter(self._client, self.stream, self.group, entry_id, entry_envelope,
                        f'not a request: kind={entry_envelope.get("kind")!r}', self._get_attempts(entry_id))
            return

        # From here the base class's own semantics, because this is the
        # part that is NOT about the envelope: bump the attempt counter,
        # ack on success, and on an exception the handler could not turn
        # into a response — a Redis publish that failed, not a delivery
        # that failed — leave the entry pending for reclaim until the
        # attempts are exhausted, then dead-letter it.
        attempt = self._bump_attempts(entry_id)
        try:
            self.handler(entry_envelope)
        except Exception as err:  # noqa: BLE001 - mirrors Consumer._process_entry
            if attempt >= self.max_attempts:
                dead_letter(self._client, self.stream, self.group, entry_id, entry_envelope,
                            f'retry exhausted: {err}', attempt)
            return
        self._client.xack(self.stream, self.group, entry_id)
        self._client.hdel(attempts_key(self.stream, self.group), entry_id)
        record_success(self._client, self.stream, self.group)


def create_librarian_consumer(redis_factory, *, consumer_name: str | None = None) -> LibrarianConsumer:
    """The librarian's subscriber, wired the way ``manage.py
    run_librarian`` runs it. The handler publishes on its own client
    rather than the consumer's read connection, which is reserved for the
    blocking read."""
    publish_client = redis_factory()

    def _handler(entry_envelope: dict[str, Any]) -> None:
        handle_request(publish_client, entry_envelope)

    return LibrarianConsumer(
        redis_factory,
        stream=REQUEST_STREAM,
        group=REQUEST_GROUP,
        consumer_name=consumer_name or os.uname().nodename,
        handler=_handler,
    )
