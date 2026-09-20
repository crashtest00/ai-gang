"""
Amended 2026-09-09 — the Jira webhook
ingestion endpoint moved here from services/scrummaster/src/server.js's deleted
`POST /webhook/jira` route. Mirrors that route's own behavior exactly
(secret check, envelope/dedupe-key shape) via Django's test Client against
the real URL routing / view / streams.publish stack and the real test
Redis container — the same rigor test_views_http_api.py already uses for
the HTTP surface.
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

from django.test import Client
from django.db import connection

from workitems import registry
from workitems.models import WorkItem
from workitems.webhook_consumer import handle_webhook_envelope

REPO_ROOT = Path(__file__).resolve().parent.parent
MANAGE_PY = REPO_ROOT / 'manage.py'
PROJECT = 'test-project'


def jira_payload(issue_key, *, event='jira:issue_created', issuetype='Task', extra_fields=None, changelog=None, timestamp=1000):
    fields = {'project': {'name': PROJECT, 'key': 'TP'}, 'summary': 'A ticket', 'issuetype': {'name': issuetype}}
    if extra_fields:
        fields.update(extra_fields)
    body = {'webhookEvent': event, 'issue': {'key': issue_key, 'fields': fields}, 'timestamp': timestamp}
    if changelog is not None:
        body['changelog'] = changelog
    return body


def test_jira_webhook_requires_the_shared_secret(clean_db, monkeypatch, redis_client):
    monkeypatch.setenv('WEBHOOK_SECRET', 's3cr3t')
    client = Client()
    resp = client.post('/webhooks/jira', data=json.dumps(jira_payload('TP-1')), content_type='application/json')
    assert resp.status_code == 401

    resp = client.post('/webhooks/jira?secret=s3cr3t', data=json.dumps(jira_payload('TP-1')), content_type='application/json')
    assert resp.status_code == 200


def test_jira_webhook_durably_enqueues_onto_the_shared_webhook_stream(clean_db, monkeypatch, redis_client):
    monkeypatch.delenv('WEBHOOK_SECRET', raising=False)
    client = Client()
    resp = client.post('/webhooks/jira', data=json.dumps(jira_payload('TP-2')), content_type='application/json')
    assert resp.status_code == 200
    assert resp.json() == {'received': True, 'deduped': False}

    stream = registry.webhook_stream_name(PROJECT)
    entries = redis_client.xrange(stream, '-', '+')
    assert len(entries) == 1
    envelope = json.loads(entries[0][1]['data'])
    assert envelope['kind'] == 'webhook_event'
    assert envelope['project'] == registry.normalize_project_name(PROJECT)
    assert envelope['payload']['issue']['key'] == 'TP-2'


def test_jira_webhook_dedupes_a_redelivered_event(clean_db, monkeypatch, redis_client):
    monkeypatch.delenv('WEBHOOK_SECRET', raising=False)
    client = Client()
    payload = jira_payload('TP-3', event='jira:issue_updated',
                            changelog={'id': 'cl-1', 'items': [{'field': 'status', 'toString': 'Done'}]})
    body = json.dumps(payload)

    first = client.post('/webhooks/jira', data=body, content_type='application/json')
    second = client.post('/webhooks/jira', data=body, content_type='application/json')
    assert first.json()['deduped'] is False
    assert second.json()['deduped'] is True

    stream = registry.webhook_stream_name(PROJECT)
    entries = redis_client.xrange(stream, '-', '+')
    assert len(entries) == 1, 'a redelivered webhook (same event/issue/timestamp/changelog id) must not double-enqueue'


def test_jira_webhook_rejects_a_payload_missing_issue(clean_db, monkeypatch):
    monkeypatch.delenv('WEBHOOK_SECRET', raising=False)
    client = Client()
    resp = client.post('/webhooks/jira', data=json.dumps({'webhookEvent': 'jira:issue_created'}), content_type='application/json')
    assert resp.status_code == 400


def test_jira_webhook_rejects_a_payload_missing_project(clean_db, monkeypatch):
    monkeypatch.delenv('WEBHOOK_SECRET', raising=False)
    client = Client()
    payload = {'webhookEvent': 'jira:issue_created', 'issue': {'key': 'TP-9', 'fields': {}}}
    resp = client.post('/webhooks/jira', data=json.dumps(payload), content_type='application/json')
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# BF-01 (v2.1/BUGFIXES.md) — REQ-01's acceptance criterion end to end: a
# Jira Release ticket's create webhook, then an update webhook with changed
# fields, both posted to the real view and consumed through the real
# webhook_consumer.handle_webhook_envelope (the exact function
# create_webhook_consumer's Streams handler wraps with zero difference —
# the durability/consumer-group mechanics around it are already covered by
# the kill-mid-batch test below and REQ-09), then read back through the
# real GET /work-items/<id>?full=true the same internal API local-mode
# releases use.
# ---------------------------------------------------------------------------

RELEASE_FIELD_IDS = {
    'JIRA_TARGET_PROJECT_FIELD_ID': 'customfield_target_project',
    'JIRA_RELEASE_NOTES_FIELD_ID': 'customfield_release_notes',
    'JIRA_CANDIDATE_SHA_FIELD_ID': 'customfield_candidate_sha',
    'JIRA_BUILD_IDENTIFIER_FIELD_ID': 'customfield_build_id',
    'JIRA_PREVIEW_URL_FIELD_ID': 'customfield_preview_url',
}


def _latest_envelope(redis_client, stream):
    entries = redis_client.xrange(stream, '-', '+')
    return json.loads(entries[-1][1]['data']), len(entries)


def test_req01_a_jira_release_ticket_materializes_the_same_release_detail_columns_local_mode_uses(
    clean_db, monkeypatch, redis_client,
):
    monkeypatch.delenv('WEBHOOK_SECRET', raising=False)
    for env_name, field_id in RELEASE_FIELD_IDS.items():
        monkeypatch.setenv(env_name, field_id)
    client = Client()
    stream = registry.webhook_stream_name(PROJECT)

    created = jira_payload(
        'REL-1', issuetype='Release',
        extra_fields={
            'customfield_target_project': {'key': 'ENG', 'name': 'engineering-app'},
            'customfield_release_notes': 'Fixes login bug.',
        },
    )
    resp = client.post('/webhooks/jira', data=json.dumps(created), content_type='application/json')
    assert resp.status_code == 200

    envelope, count = _latest_envelope(redis_client, stream)
    assert count == 1
    handle_webhook_envelope(envelope)

    item = WorkItem.objects.get(external_key='REL-1')
    assert item.type == 'release'

    read_after_create = client.get(f'/work-items/{item.id}', {'full': 'true'})
    assert read_after_create.status_code == 200
    detail_after_create = read_after_create.json()['releaseDetail']
    assert detail_after_create['release_notes'] == 'Fixes login bug.'
    assert detail_after_create['candidate_sha'] is None
    assert detail_after_create['build_identifier'] is None
    assert detail_after_create['preview_url'] is None

    # The release-candidate Jenkins job writes Candidate SHA, Build
    # Identifier, and Preview URL back onto the ticket once cut
    # (scripts/create-release-fields.sh) — an ordinary issue_updated
    # webhook naming the changed field.
    updated = jira_payload(
        'REL-1', event='jira:issue_updated', issuetype='Release',
        extra_fields={
            'customfield_target_project': {'key': 'ENG', 'name': 'engineering-app'},
            'customfield_release_notes': 'Fixes login bug.',
            'customfield_candidate_sha': 'abc1234',
            'customfield_build_id': 'build-42',
            'customfield_preview_url': 'https://preview.example.com/abc1234',
        },
        changelog={'id': 'cl-rel-1', 'items': [
            {'field': 'Candidate SHA', 'fieldId': 'customfield_candidate_sha', 'from': None, 'to': 'abc1234'},
        ]},
        timestamp=2000,
    )
    resp2 = client.post('/webhooks/jira', data=json.dumps(updated), content_type='application/json')
    assert resp2.status_code == 200

    envelope2, count2 = _latest_envelope(redis_client, stream)
    assert count2 == 2
    handle_webhook_envelope(envelope2)

    read_after_update = client.get(f'/work-items/{item.id}', {'full': 'true'})
    detail_after_update = read_after_update.json()['releaseDetail']
    assert detail_after_update['candidate_sha'] == 'abc1234'
    assert detail_after_update['build_identifier'] == 'build-42'
    assert detail_after_update['preview_url'] == 'https://preview.example.com/abc1234'
    assert detail_after_update['release_notes'] == 'Fixes login bug.'


def test_req01_an_update_webhook_for_a_release_ticket_with_no_prior_create_still_materializes_it(
    clean_db, monkeypatch, redis_client,
):
    """BUGFIXES.md BF-01 Pass 1 audit row 2: a Release ticket created
    before BF-01 shipped (or whose jira:issue_created webhook was never
    delivered/consumed) has no canonical `release` work item yet — only
    `jira:issue_updated` webhooks are arriving for it now. REQ-01's
    "create OR update webhook" acceptance criterion means the update path
    alone must still materialize the same `release` work item and
    `work_item_release_detail` row, exercised here through the real
    dispatch path: POST /webhooks/jira -> the durable stream ->
    handle_webhook_envelope -> GET /work-items/<id>?full=true, exactly
    like the create-then-update test above."""
    monkeypatch.delenv('WEBHOOK_SECRET', raising=False)
    for env_name, field_id in RELEASE_FIELD_IDS.items():
        monkeypatch.setenv(env_name, field_id)
    client = Client()
    stream = registry.webhook_stream_name(PROJECT)

    updated = jira_payload(
        'REL-2', event='jira:issue_updated', issuetype='Release',
        extra_fields={
            'customfield_target_project': {'key': 'ENG', 'name': 'engineering-app'},
            'customfield_release_notes': 'Fixes the checkout bug.',
            'customfield_candidate_sha': 'def5678',
        },
        changelog={'id': 'cl-rel-2', 'items': [
            {'field': 'Candidate SHA', 'fieldId': 'customfield_candidate_sha', 'from': None, 'to': 'def5678'},
        ]},
    )
    resp = client.post('/webhooks/jira', data=json.dumps(updated), content_type='application/json')
    assert resp.status_code == 200

    envelope, count = _latest_envelope(redis_client, stream)
    assert count == 1
    handle_webhook_envelope(envelope)

    item = WorkItem.objects.get(external_key='REL-2')
    assert item.type == 'release'
    assert item.project == 'engineering-app'

    read = client.get(f'/work-items/{item.id}', {'full': 'true'})
    assert read.status_code == 200
    detail = read.json()['releaseDetail']
    assert detail['release_notes'] == 'Fixes the checkout bug.'
    assert detail['candidate_sha'] == 'def5678'

    # A second update for the same never-created ticket must not create a
    # duplicate work item or detail row.
    updated_again = jira_payload(
        'REL-2', event='jira:issue_updated', issuetype='Release',
        extra_fields={
            'customfield_target_project': {'key': 'ENG', 'name': 'engineering-app'},
            'customfield_release_notes': 'Fixes the checkout bug.',
            'customfield_candidate_sha': 'def5678',
            'customfield_build_id': 'build-7',
        },
        changelog={'id': 'cl-rel-3', 'items': [
            {'field': 'Build Identifier', 'fieldId': 'customfield_build_id', 'from': None, 'to': 'build-7'},
        ]},
        timestamp=2000,
    )
    resp2 = client.post('/webhooks/jira', data=json.dumps(updated_again), content_type='application/json')
    assert resp2.status_code == 200
    envelope2, count2 = _latest_envelope(redis_client, stream)
    assert count2 == 2
    handle_webhook_envelope(envelope2)

    assert WorkItem.objects.filter(external_key='REL-2').count() == 1
    read_again = client.get(f'/work-items/{item.id}', {'full': 'true'})
    assert read_again.json()['releaseDetail']['build_identifier'] == 'build-7'


# ---------------------------------------------------------------------------
# Acceptance: "Kill Django/work-item-service immediately after a
# webhook is durably enqueued but before it's processed; on restart, the
# event is processed exactly once, without requiring Jira to re-fire it."
# Mirrors tests/test_relay_integration.py's rigor: a REAL child process,
# genuinely SIGKILLed mid-batch (proven via partial progress before the
# kill), not a mocked/simulated crash.
# ---------------------------------------------------------------------------

def _consumer_env(**overrides):
    env = dict(os.environ)
    env.setdefault('PGHOST', os.environ.get('PGHOST', 'localhost'))
    env.setdefault('PGPORT', os.environ.get('PGPORT', '15432'))
    env.setdefault('PGUSER', os.environ.get('PGUSER', 'workitem'))
    env.setdefault('PGPASSWORD', os.environ.get('PGPASSWORD', 'workitem'))
    env['PGDATABASE'] = connection.settings_dict['NAME']
    env.setdefault('REDIS_URL', os.environ.get('REDIS_TEST_URL', 'redis://localhost:16399'))
    env.setdefault('AGENTS_CATALOG_PATH', os.environ['AGENTS_CATALOG_PATH'])
    env.setdefault('PROJECTS_CONFIG_PATH', os.environ['PROJECTS_CONFIG_PATH'])
    env.update(overrides)
    return env


def spawn_consumers(**extra_env):
    env = _consumer_env(
        # A dead consumer's pending (unacked) entries are only picked up by
        # the reclaim loop once idle past retry_delay_ms — production
        # defaults (60s / 10min) exist so a message merely taking a while
        # isn't wrongly reclaimed, but this test needs the second instance
        # to pick up the first's unacked entry in seconds, not minutes.
        STREAM_RECLAIM_INTERVAL_MS='300', STREAM_RETRY_DELAY_MS='300', **extra_env,
    )
    return subprocess.Popen(
        [sys.executable, str(MANAGE_PY), 'run_consumers', '--consumer-id', 'kill-mid-ingest'],
        cwd=str(REPO_ROOT), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def wait_for(predicate, timeout_s=20.0, interval_s=0.1):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(interval_s)
    raise TimeoutError('wait_for timed out')


def _created_count(project):
    return WorkItem.objects.filter(project=project, type='story').count()


def test_req09_killing_the_webhook_consumer_mid_batch_and_restarting_processes_each_event_exactly_once(
    clean_db, redis_client, monkeypatch,
):
    monkeypatch.delenv('WEBHOOK_SECRET', raising=False)
    # Must be a project the fixture catalog (services/scrummaster/test/fixtures/
    # projects.json) actually knows about — run_consumers only starts a
    # consumer for each of registry.get_project_names(), so an unregistered
    # project name would sit unconsumed forever regardless of this test.
    project = PROJECT
    client = Client()

    total = 6
    for i in range(total):
        payload = jira_payload(f'KMI-{i}', event='jira:issue_created', issuetype='Story',
                                extra_fields={'project': {'name': project, 'key': 'KMI'}, 'summary': f'Story {i}'})
        resp = client.post('/webhooks/jira', data=json.dumps(payload), content_type='application/json')
        assert resp.status_code == 200

    stream = registry.webhook_stream_name(project)
    assert redis_client.xlen(stream) == total, 'every webhook must be durably enqueued before any consumer runs'

    # First consumer instance: an artificial per-event delay makes this
    # batch slow enough to reliably SIGKILL partway through.
    first = spawn_consumers(WEBHOOK_CONSUMER_ROW_DELAY_MS='700')
    try:
        wait_for(lambda: _created_count(project) >= 1)
        mid_run_count = _created_count(project)
        assert mid_run_count < total, f'expected a partial batch at kill time, got {mid_run_count}/{total} already processed'
        first.send_signal(signal.SIGKILL)
    finally:
        first.wait(timeout=10)

    # Restart with no artificial delay — drains the remainder quickly, with
    # no Jira redelivery involved (the events are still sitting, durably,
    # on the Streams entry this consumer never acked).
    second = spawn_consumers(WEBHOOK_CONSUMER_ROW_DELAY_MS='0')
    try:
        wait_for(lambda: _created_count(project) == total, timeout_s=20.0)
    finally:
        second.send_signal(signal.SIGKILL)
        second.wait(timeout=10)

    # Exactly once: no duplicate Story rows for the same Jira issue key.
    external_keys = list(WorkItem.objects.filter(project=project).values_list('external_key', flat=True))
    assert sorted(external_keys) == sorted(f'KMI-{i}' for i in range(total))
    assert len(external_keys) == len(set(external_keys)), 'no issue key must have been processed twice'
