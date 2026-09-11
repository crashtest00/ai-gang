"""Mirrors services/work-item-service/test/webhookConsumer.test.js."""

from __future__ import annotations

import time
import uuid

from workitems import project_config, registry, store
from workitems.envelope import Kind, build_envelope
from workitems.models import WebhookFailure
from workitems.streams import ensure_group, publish
from workitems.webhook_consumer import create_webhook_consumer

PROJECT = 'test-project'


def wait_for(predicate, timeout_s=5.0, interval_s=0.03):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(interval_s)
    raise TimeoutError('wait_for timed out')


def jira_webhook_envelope(issue_key, to_status):
    return build_envelope(Kind.WEBHOOK_EVENT, registry.normalize_project_name(PROJECT), payload={
        'event': 'jira:issue_updated',
        'issue': {'key': issue_key},
        'body': {'changelog': {'items': [{'field': 'status', 'toString': to_status}]}},
    })


def test_req11_validated_jira_status_change_is_applied(clean_db, redis_client, redis_factory):
    item_id = uuid.uuid4()
    store.create_work_item({'id': item_id, 'project': PROJECT, 'type': 'task', 'displayName': 'X', 'externalKey': 'TP-1'})
    project_config.set_mode(PROJECT, 'jira')

    consumer = create_webhook_consumer(redis_factory, PROJECT, consumer_name='wh-1')
    consumer.start()
    try:
        stream = registry.webhook_stream_name(PROJECT)
        publish(redis_client, stream, jira_webhook_envelope('TP-1', 'In Progress'))
        wait_for(lambda: store.get_work_item(item_id).status == 'in-progress')
    finally:
        consumer.stop()


def test_req11_jira_change_violating_dependency_gate_is_rejected_and_recorded(clean_db, redis_client, redis_factory):
    blocker = uuid.uuid4()
    item_id = uuid.uuid4()
    store.create_work_item({'id': blocker, 'project': PROJECT, 'type': 'task', 'displayName': 'Blocker'})
    store.create_work_item({'id': item_id, 'project': PROJECT, 'type': 'task', 'displayName': 'Dependent', 'externalKey': 'TP-2'})
    store.create_link(blocker, item_id, 'blocks')

    project_config.set_mode(PROJECT, 'jira')

    consumer = create_webhook_consumer(redis_factory, PROJECT, consumer_name='wh-2')
    consumer.start()
    try:
        stream = registry.webhook_stream_name(PROJECT)
        # "Shovel Ready" maps to canonical 'ready' — the dependent's blocker
        # is still incomplete, so this must be rejected, not applied.
        publish(redis_client, stream, jira_webhook_envelope('TP-2', 'Shovel Ready'))

        wait_for(lambda: WebhookFailure.objects.filter(work_item_id=item_id).count() == 1)
    finally:
        consumer.stop()

    item = store.get_work_item(item_id)
    assert item.status == 'proposed', 'the invalid transition must never have been applied'


def test_req02_per_project_jira_status_mapping_overrides_the_default_map(clean_db, redis_client, redis_factory):
    item_id = uuid.uuid4()
    store.create_work_item({'id': item_id, 'project': PROJECT, 'type': 'task', 'displayName': 'X', 'externalKey': 'TP-4'})
    project_config.set_mode(PROJECT, 'jira')
    # This project maps its own "Doing" Jira status to canonical 'in-review'
    # rather than the built-in default map's 'in-progress' for a similarly-
    # named status — proves the per-project config, not the hardcoded
    # literal, decides the outcome.
    project_config.declare_custom_status(PROJECT, 'in-review', 'in-review', jira_status_name='Doing')

    consumer = create_webhook_consumer(redis_factory, PROJECT, consumer_name='wh-4')
    consumer.start()
    try:
        stream = registry.webhook_stream_name(PROJECT)
        publish(redis_client, stream, jira_webhook_envelope('TP-4', 'Doing'))
        wait_for(lambda: store.get_work_item(item_id).status == 'in-review')
    finally:
        consumer.stop()


def test_req12_jira_mode_project_feeds_existing_scrummaster_webhook_group_unchanged(clean_db, redis_client, redis_factory):
    # Publish once; both this service's group AND a stand-in for
    # ScrumMaster's own "scrummaster" group must each independently see the
    # entry — proving this is additive fan-out on the same durable stream,
    # not a takeover.
    stream = registry.webhook_stream_name(PROJECT)
    ensure_group(redis_client, stream, registry.WEBHOOK_GROUP)  # simulates server.js's existing group
    publish(redis_client, stream, jira_webhook_envelope('TP-3', 'Done'))

    consumer = create_webhook_consumer(redis_factory, PROJECT, consumer_name='wh-3')
    consumer.start()
    try:
        # This service's own "workitemservice" group independently drains the entry.
        wait_for(lambda: (redis_client.xpending(stream, 'workitemservice') or {}).get('pending', 0) == 0)
    finally:
        consumer.stop()

    # The pre-existing "scrummaster" group's own view is untouched.
    scrummaster_summary = redis_client.xpending(stream, registry.WEBHOOK_GROUP)
    assert (scrummaster_summary or {}).get('pending', 0) == 0
    raw = redis_client.xinfo_groups(stream)
    group_names = [g['name'] for g in raw]
    assert registry.WEBHOOK_GROUP in group_names, 'the pre-existing ScrumMaster group still exists on the stream'
    assert 'workitemservice' in group_names, "this service's own group also exists, independently"
