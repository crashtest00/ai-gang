"""
internal-work-item-service.md REQ-06's own acceptance criterion, verified
for real rather than simulated in-process — mirrors
services/work-item-service/test/relay.integration.test.js exactly, at the same
rigor: "Kill the outbox relay process mid-run after several datastore
writes have committed. On restart, every committed write's event is
eventually published exactly once — none missing, none duplicated as a
distinct logical event."

This spawns `python manage.py relay` as a REAL child process (not a
mocked timer or an in-process function call), SIGKILLs it while it is
provably partway through a batch, then starts a fresh instance and
confirms every committed outbox row's event ends up on the Redis stream
exactly once. This is the single most load-bearing test in the whole
service (per the task brief) — match or exceed the Node original's rigor.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

from django.db import connection, transaction

from workitems.stream_topology import event_stream_name
from workitems.store import write_outbox_event

REPO_ROOT = Path(__file__).resolve().parent.parent
MANAGE_PY = REPO_ROOT / 'manage.py'
PROJECT = 'kill-mid-run-project-django'


def _relay_env(**overrides):
    env = dict(os.environ)
    env.setdefault('PGHOST', os.environ.get('PGHOST', 'localhost'))
    env.setdefault('PGPORT', os.environ.get('PGPORT', '15432'))
    env.setdefault('PGUSER', os.environ.get('PGUSER', 'workitem'))
    env.setdefault('PGPASSWORD', os.environ.get('PGPASSWORD', 'workitem'))
    # Critical: pytest-django runs the test suite against a `test_`-prefixed
    # database it creates for the session (Django's normal test-database
    # behavior) — NOT the plain PGDATABASE env var's database. The relay
    # subprocess must connect to that SAME database (the one this test's
    # own writes just committed to), or it will find no unpublished rows at
    # all. `connection.settings_dict['NAME']` is the actual, live database
    # name Django is using in THIS process right now.
    env['PGDATABASE'] = connection.settings_dict['NAME']
    env.setdefault('REDIS_URL', os.environ.get('REDIS_TEST_URL', 'redis://localhost:16399'))
    env['RELAY_POLL_INTERVAL_MS'] = '100'
    env['RELAY_BATCH_SIZE'] = '20'
    env.update(overrides)
    return env


def spawn_relay(**extra_env):
    return subprocess.Popen(
        [sys.executable, str(MANAGE_PY), 'relay'],
        cwd=str(REPO_ROOT),
        env=_relay_env(**extra_env),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def published_count():
    from workitems.models import OutboxEvent
    return OutboxEvent.objects.filter(project=PROJECT, published_at__isnull=False).count()


def wait_for(predicate, timeout_s=20.0, interval_s=0.1):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(interval_s)
    raise TimeoutError('wait_for timed out')


def test_req06_killing_relay_mid_batch_and_restarting_loses_nothing_and_duplicates_nothing(clean_db, redis_client):
    total = 8
    outbox_ids = []
    for i in range(total):
        work_item_id = uuid.uuid4()
        with transaction.atomic():
            outbox_id = write_outbox_event(
                project=PROJECT, event_type='work_item.status_changed', work_item_id=work_item_id, payload={'seq': i},
            )
            outbox_ids.append(outbox_id)

    # First relay instance: an artificial 400ms delay between rows makes a
    # ~3.2s batch — long enough to reliably SIGKILL it partway through
    # rather than racing a batch that finishes before the kill signal is
    # sent.
    first = spawn_relay(RELAY_ROW_DELAY_MS='400')
    try:
        # Prove this is genuinely a MID-RUN kill: wait until at least one
        # row has published, then confirm not everything has published yet
        # before killing.
        wait_for(lambda: published_count() >= 1)
        mid_run_count = published_count()
        assert mid_run_count < total, f'expected a partial batch at kill time, got {mid_run_count}/{total} already published'

        first.send_signal(signal.SIGKILL)  # a real, unrecoverable kill, not SIGTERM.
    finally:
        first.wait(timeout=10)

    # Restart with no artificial delay — drains the remainder quickly.
    second = spawn_relay(RELAY_ROW_DELAY_MS='0')
    try:
        wait_for(lambda: published_count() == total, timeout_s=20.0)
    finally:
        second.send_signal(signal.SIGKILL)
        second.wait(timeout=10)

    # No missed events: every outbox row is marked published.
    from workitems.models import OutboxEvent
    unpublished = list(OutboxEvent.objects.filter(project=PROJECT, published_at__isnull=True))
    assert unpublished == []

    # No duplicated logical events: exactly one Streams entry per outbox
    # row, by distinct messageId (the outbox row's own id — see relay.py).
    stream = event_stream_name(PROJECT)
    entries = redis_client.xrange(stream, '-', '+')
    message_ids = [json.loads(fields['data'])['messageId'] for _entry_id, fields in entries]
    unique_message_ids = set(message_ids)
    assert len(message_ids) == total, f'expected exactly {total} Streams entries, got {len(message_ids)}'
    assert len(unique_message_ids) == total, 'no duplicated logical event (same messageId appearing twice)'
    assert sorted(unique_message_ids) == sorted(outbox_ids)
