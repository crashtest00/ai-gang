"""
Durable Artifact Event History REQ-04 — the librarian's one answer per
request is durable before the request is acknowledged.

Every request here travels the real path: a real envelope on the real
``aigang:librarian:requests`` stream in the test Redis, read by the real
consumer (``librarian_env``, the same subscriber ``manage.py run_librarian``
starts), with its answer committed to the outbox and relayed by the real
``workitems.relay.relay_once``. A second handling of one request is a real
reclaim by a second subscriber (``workitems.streams``' ``XAUTOCLAIM``), made
prompt by shortening that subscriber's retry delay rather than by calling
the handler directly.

The only seams are the ones the Acceptance names: an acknowledgement made
to fail once after the commit, a handling made to fail before ``deliver``'s
lock, the outbox insert made to fail, the lookup patched to report no row,
and the failure answer's write made to raise ``OperationalError``.
"""

from __future__ import annotations

import json
import socket
import threading
import time
import uuid

import pytest
import redis as redis_lib
from django.db import DatabaseError, IntegrityError, OperationalError, connection, transaction

from librarian import consumer as librarian_consumer
from librarian import responses
from librarian.delivery import advisory_lock_key
from librarian.envelope import REQUEST_KIND, RESPONSE_KIND, build_envelope, to_stream_fields
from librarian.failures import COPY_FAILED, DeliveryFailure
from librarian.models import ArtifactDelivery
from librarian.stream_topology import REQUEST_GROUP, REQUEST_STREAM, RESPONSE_STREAM
from workitems import store
from workitems.models import OutboxEvent
from workitems.relay import relay_once

from tests.librarian_support import (  # noqa: F401 - librarian_env is a fixture
    await_lock_granted, await_response, drain_outbox, files_in, last_response_id, librarian_env, make_repo,
    seed_artifact, start_consumer,
)

MOCKUP = b'\x89PNG\r\n\x1a\n-pretend-this-is-a-mockup-'
REQUESTER_TIMEOUT_SECONDS = 30  # request-artifact.js's DEFAULT_TIMEOUT_MS


# --- helpers ----------------------------------------------------------------

def _until(predicate, *, timeout=20.0, what='the condition'):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(f'timed out waiting for {what}')


def _publish(env, payload: dict) -> tuple[str, str]:
    cursor = last_response_id(env.redis)
    envelope = build_envelope(REQUEST_KIND, payload=payload)
    env.redis.xadd(REQUEST_STREAM, to_stream_fields(envelope))
    return envelope['messageId'], cursor


def _request(artifact_id, repo='hello-web', path='designs/mockup.png') -> dict:
    return {'requestedBy': 'frontend-agent', 'artifactId': str(artifact_id),
            'destinationRepo': repo, 'requestedPath': path}


def _answer_rows(message_id=None):
    rows = OutboxEvent.objects.filter(stream=RESPONSE_STREAM)
    if message_id is not None:
        rows = [r for r in rows if r.envelope.get('correlationId') == message_id]
    return list(rows)


def _answers_on_stream(env, message_id):
    drain_outbox(env.redis)
    return [json.loads(f['data']) for _id, f in env.redis.xrange(RESPONSE_STREAM)
            if json.loads(f['data']).get('correlationId') == message_id]


def _pending(env) -> int:
    return env.redis.xpending(REQUEST_STREAM, REQUEST_GROUP)['pending']


def _reclaim_promptly(consumer):
    """Make a running subscriber reclaim an idle entry within a second
    instead of after ``DEFAULT_RETRY_DELAY_MS``. Both are read on every
    pass of its reclaim loop."""
    consumer.retry_delay_ms = 300
    consumer.reclaim_interval_ms = 250


def _fail_next_ack(consumer) -> list:
    """Make the subscriber's next ``XACK`` on the request stream raise —
    the state a kill between the handler's commit and the acknowledgement
    leaves. Returns the list of acknowledgement outcomes, for the test to
    observe the reclaim's own acknowledgement."""
    real_xack = consumer._client.xack
    outcomes = []

    def xack(stream, group, *ids):
        if stream == REQUEST_STREAM and not outcomes:
            outcomes.append('failed')
            raise redis_lib.exceptions.ConnectionError('acknowledgement lost after the commit')
        outcomes.append('acked')
        return real_xack(stream, group, *ids)

    consumer._client.xack = xack
    return outcomes


def _lock_holders(key: int) -> dict:
    """``pg_locks`` rows for one advisory-lock key: {'granted': [pid], 'waiting': [pid]}."""
    classid, objid = (key >> 32) & 0xFFFFFFFF, key & 0xFFFFFFFF
    with connection.cursor() as cursor:
        cursor.execute("SELECT pid, granted FROM pg_locks WHERE locktype = 'advisory' AND classid = %s "
                       "AND objid = %s AND objsubid = 1", [classid, objid])
        rows = cursor.fetchall()
    return {'granted': [p for p, g in rows if g], 'waiting': [p for p, g in rows if not g]}


def _fail_second_handling_before_deliver(monkeypatch, message_id) -> list:
    """Every handling of ``message_id`` after the first fails before it
    reaches ``deliver``'s lock, as an unresolvable repository or a missing
    source file would. Returns the handlings seen."""
    real_deliver = librarian_consumer.deliver
    seen = []
    guard = threading.Lock()

    def deliver(request, *, correlation_id):
        with guard:
            seen.append(correlation_id)
            later = correlation_id == message_id and seen.count(message_id) > 1
        if later:
            raise DeliveryFailure(COPY_FAILED, 'this handling failed before reaching the lock')
        return real_deliver(request, correlation_id=correlation_id)

    monkeypatch.setattr(librarian_consumer, 'deliver', deliver)
    return seen


# --- durable before acknowledged, and relayed within the requester's wait ---

def _redis_that_is_down():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    return redis_lib.Redis.from_url(f'redis://127.0.0.1:{port}/0', decode_responses=True, socket_connect_timeout=2)


@pytest.mark.parametrize('kind', ['confirmation', 'failure'])
def test_an_answer_is_committed_and_the_request_acknowledged_while_the_relay_cannot_publish(librarian_env, kind):
    artifact = seed_artifact(MOCKUP)
    repo = make_repo(librarian_env, 'hello-web')
    drain_outbox(librarian_env.redis)  # the upload's own announcement
    started = time.time()
    message_id, cursor = _publish(librarian_env, _request(
        artifact.id, repo='hello-web' if kind == 'confirmation' else 'no-such-repo'))

    _until(lambda: _answer_rows(message_id) and _pending(librarian_env) == 0,
           what='the answer row and the acknowledgement')
    (row,) = _answer_rows(message_id)
    assert row.published_at is None
    assert row.envelope['kind'] == RESPONSE_KIND
    if kind == 'confirmation':
        assert files_in(repo) == ['designs/mockup.png']
        assert ArtifactDelivery.objects.filter(artifact_id=artifact.id, repository='hello-web').exists()
        assert row.envelope['payload']['status'] == 'delivered'
    else:
        assert row.envelope['payload']['status'] == 'failed'
        assert row.envelope['payload']['reason'] == 'unknown_destination_repo'
    with pytest.raises(redis_lib.exceptions.ConnectionError):
        relay_once(_redis_that_is_down())
    assert librarian_env.redis.xlen(RESPONSE_STREAM) == 0

    answer = await_response(librarian_env, message_id, cursor)

    assert time.time() - started < REQUESTER_TIMEOUT_SECONDS
    assert answer['messageId'] == row.envelope['messageId']
    assert len(_answers_on_stream(librarian_env, message_id)) == 1


# --- a lost acknowledgement does not produce a second answer ---------------

@pytest.mark.parametrize('kind', ['confirmation', 'failure'])
def test_an_acknowledgement_that_fails_after_the_commit_is_reclaimed_without_a_second_answer(librarian_env, kind):
    artifact = seed_artifact(MOCKUP)
    make_repo(librarian_env, 'hello-web')
    consumer = librarian_env.consumers[0]
    acks = _fail_next_ack(consumer)

    message_id, _cursor = _publish(librarian_env, _request(
        artifact.id, repo='hello-web' if kind == 'confirmation' else 'no-such-repo'))
    _until(lambda: acks == ['failed'], what='the failed acknowledgement')
    assert _pending(librarian_env) == 1
    assert len(_answer_rows(message_id)) == 1

    _reclaim_promptly(consumer)
    _until(lambda: _pending(librarian_env) == 0, what='the reclaimed handling to acknowledge')

    assert acks == ['failed', 'acked']
    assert len(_answer_rows(message_id)) == 1
    answers = _answers_on_stream(librarian_env, message_id)
    assert len(answers) == 1
    assert answers[0]['payload']['status'] == ('delivered' if kind == 'confirmation' else 'failed')


def test_a_request_first_answered_with_a_failure_is_never_delivered_later(librarian_env):
    artifact = seed_artifact(MOCKUP)
    consumer = librarian_env.consumers[0]
    acks = _fail_next_ack(consumer)

    message_id, _cursor = _publish(librarian_env, _request(artifact.id))  # hello-web does not exist yet
    _until(lambda: acks == ['failed'], what='the failure answer and its failed acknowledgement')
    repo = make_repo(librarian_env, 'hello-web')  # the request is now satisfiable

    _reclaim_promptly(consumer)
    _until(lambda: _pending(librarian_env) == 0, what='the reclaimed handling to acknowledge')

    answers = _answers_on_stream(librarian_env, message_id)
    assert [a['payload']['status'] for a in answers] == ['failed']
    assert answers[0]['payload']['reason'] == 'unknown_destination_repo'
    assert files_in(repo) == []
    assert not ArtifactDelivery.objects.exists()


# --- the partial unique index ----------------------------------------------

def _answer_envelope(correlation_id, status='delivered'):
    return build_envelope(RESPONSE_KIND, payload={'status': status, 'artifactId': str(uuid.uuid4())},
                          correlation_id=correlation_id)


def test_the_partial_unique_index_refuses_a_second_answer_for_one_request_only(clean_db):
    correlation_id = f'msg-{uuid.uuid4()}'
    with transaction.atomic():
        store.write_outbox_event(stream=RESPONSE_STREAM, envelope=_answer_envelope(correlation_id))

    with pytest.raises(IntegrityError) as refused:
        with transaction.atomic():
            store.write_outbox_event(stream=RESPONSE_STREAM, envelope=_answer_envelope(correlation_id, 'failed'))
    assert responses.is_answer_refusal(refused.value)

    with transaction.atomic():
        store.write_outbox_event(stream='aigang:artifacts:events', envelope=_answer_envelope(correlation_id))
    assert OutboxEvent.objects.count() == 2


# --- a failed insert in deliver's transaction records nothing -------------

def test_an_outbox_insert_that_fails_inside_deliver_leaves_no_delivery_record(librarian_env, monkeypatch):
    artifact = seed_artifact(MOCKUP)
    make_repo(librarian_env, 'hello-web')
    real_write = responses.write_outbox_event

    def write(**kwargs):
        if kwargs['envelope']['payload'].get('status') == 'delivered':
            raise DatabaseError('the outbox insert failed')
        return real_write(**kwargs)

    monkeypatch.setattr(responses, 'write_outbox_event', write)
    message_id, cursor = _publish(librarian_env, _request(artifact.id))

    answer = await_response(librarian_env, message_id, cursor)

    assert answer['payload']['status'] == 'failed'
    assert not ArtifactDelivery.objects.exists()
    assert [r.envelope['payload']['status'] for r in _answer_rows(message_id)] == ['failed']


# --- two handlings of one request serialize on the request's lock ----------

def test_a_failing_second_handling_waits_for_the_first_and_adds_no_answer(librarian_env, settings, monkeypatch):
    settings.LIBRARIAN_LOCK_HOLD_DELAY_MS = 4000
    artifact = seed_artifact(MOCKUP)
    make_repo(librarian_env, 'hello-web')
    message_id, _cursor = _publish(librarian_env, _request(artifact.id))
    seen = _fail_second_handling_before_deliver(monkeypatch, message_id)
    answer_key = responses.answer_lock_key(message_id)

    await_lock_granted(str(artifact.id), 'hello-web')
    _until(lambda: len(_lock_holders(answer_key)['granted']) == 1, what="the first handling's request lock")
    second = start_consumer(librarian_env, 'librarian-test-2')
    _reclaim_promptly(second)

    _until(lambda: len(_lock_holders(answer_key)['waiting']) == 1, what="the failure transaction to wait")
    assert seen.count(message_id) == 2
    assert not _answer_rows(message_id), 'nothing is answered while the first handling holds the lock'

    _until(lambda: _pending(librarian_env) == 0 and not _lock_holders(answer_key)['waiting'],
           what='both handlings to finish')
    answers = _answers_on_stream(librarian_env, message_id)
    assert [a['payload']['status'] for a in answers] == ['delivered']
    assert ArtifactDelivery.objects.filter(artifact_id=artifact.id, repository='hello-web').exists()


def test_the_two_locks_are_always_taken_in_one_order(librarian_env, settings, monkeypatch):
    """A first handling of request A holds both of ``deliver``'s locks; a
    request B for the same artifact and repository waits on the
    (artifact, repository) lock; a reclaimed handling of A fails before
    ``deliver`` and waits on A's request lock while holding no
    (artifact, repository) lock. Nothing deadlocks."""
    settings.LIBRARIAN_LOCK_HOLD_DELAY_MS = 4000
    artifact = seed_artifact(MOCKUP)
    make_repo(librarian_env, 'hello-web')
    pair_key = advisory_lock_key(str(artifact.id), 'hello-web')

    a_id, a_cursor = _publish(librarian_env, _request(artifact.id, path='designs/a.png'))
    _fail_second_handling_before_deliver(monkeypatch, a_id)
    await_lock_granted(str(artifact.id), 'hello-web')

    start_consumer(librarian_env, 'librarian-test-2')
    b_id, b_cursor = _publish(librarian_env, _request(artifact.id, path='designs/b.png'))
    _until(lambda: len(_lock_holders(pair_key)['waiting']) == 1, what='request B to wait on the pair lock')

    third = start_consumer(librarian_env, 'librarian-test-3')
    _reclaim_promptly(third)
    a_key = responses.answer_lock_key(a_id)
    _until(lambda: len(_lock_holders(a_key)['waiting']) == 1, what="A's failure transaction to wait")
    (failure_pid,) = _lock_holders(a_key)['waiting']
    pair = _lock_holders(pair_key)
    assert failure_pid not in pair['granted'] + pair['waiting'], \
        'the failure transaction holds no (artifact, repository) lock while it waits'

    a_answer = await_response(librarian_env, a_id, a_cursor, timeout=30)
    b_answer = await_response(librarian_env, b_id, b_cursor, timeout=30)
    _until(lambda: _pending(librarian_env) == 0, what='every handling to finish')

    assert a_answer['payload']['status'] == 'delivered'
    assert b_answer['payload']['status'] == 'delivered'
    for message_id in (a_id, b_id):
        answers = _answers_on_stream(librarian_env, message_id)
        assert len(answers) == 1
        assert 'deadlock' not in json.dumps(answers[0]).lower()
        assert '40P01' not in json.dumps(answers[0])


# --- a refused insert is an answered request, not a failure ----------------

def test_an_insert_the_index_refuses_is_acknowledged_with_no_new_answer(librarian_env, monkeypatch):
    artifact = seed_artifact(MOCKUP)
    repo = make_repo(librarian_env, 'hello-web')
    request_envelope = build_envelope(REQUEST_KIND, payload=_request(artifact.id))
    message_id = request_envelope['messageId']
    with transaction.atomic():
        existing = store.write_outbox_event(
            stream=RESPONSE_STREAM,
            envelope=build_envelope(RESPONSE_KIND, payload={'status': 'delivered', 'path': 'elsewhere.png'},
                                    correlation_id=message_id),
        )
    monkeypatch.setattr(responses, 'already_answered', lambda correlation_id: False)

    librarian_env.redis.xadd(REQUEST_STREAM, to_stream_fields(request_envelope))
    _until(lambda: librarian_env.redis.xinfo_groups(REQUEST_STREAM)[0]['last-delivered-id'] != '0-0'
           and _pending(librarian_env) == 0, what='the request to be handled and acknowledged')

    assert [str(r.id) for r in OutboxEvent.objects.filter(stream=RESPONSE_STREAM)] == [existing]
    assert not any(r.envelope['payload'].get('reason') == COPY_FAILED for r in _answer_rows())
    assert not ArtifactDelivery.objects.exists()
    assert files_in(repo) == ['designs/mockup.png'], 'the copy step 5 made stays (§4)'


# --- a database that cannot take the failure answer -------------------------

def test_a_failure_answer_the_database_cannot_take_dead_letters_the_request_unanswered(librarian_env, monkeypatch):
    real_write = responses.write_outbox_event
    attempts = []

    def write(**kwargs):
        if kwargs['envelope']['payload'].get('status') == 'failed':
            attempts.append(time.time())
            raise OperationalError('the database cannot take the answer')
        return real_write(**kwargs)

    monkeypatch.setattr(responses, 'write_outbox_event', write)
    message_id, _cursor = _publish(librarian_env, _request(uuid.uuid4()))

    _until(lambda: len(attempts) == 1, what='the first attempt')
    time.sleep(0.3)
    assert _pending(librarian_env) == 1
    assert librarian_env.redis.xlen(f'{REQUEST_STREAM}:dead') == 0
    drain_outbox(librarian_env.redis)
    assert librarian_env.redis.xlen(RESPONSE_STREAM) == 0

    _reclaim_promptly(librarian_env.consumers[0])
    _until(lambda: librarian_env.redis.xlen(f'{REQUEST_STREAM}:dead') == 1, timeout=30,
           what='the third failed attempt to dead-letter the request')

    assert len(attempts) == 3
    dead = json.loads(librarian_env.redis.xrange(f'{REQUEST_STREAM}:dead')[0][1]['data'])
    assert dead['envelope']['messageId'] == message_id
    assert dead['reason'].startswith('retry exhausted')
    assert _pending(librarian_env) == 0
    drain_outbox(librarian_env.redis)
    assert librarian_env.redis.xlen(RESPONSE_STREAM) == 0
    assert not _answer_rows()
