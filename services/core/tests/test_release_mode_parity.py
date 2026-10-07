"""
release-mode-parity.md REQ-09 … REQ-14 — a Release behaves the same in both
tracker modes.

Every test drives a production entry point: local mode through the Streams
command handler (`command_consumer.handle_command` / `_handler`) and the
external HTTP API the release-candidate job calls; Jira mode through
`webhook_consumer.handle_webhook_envelope`, the same HTTP API, the real relay
and `jira_writer.handle_event_envelope`, against `tests/jira_fixture.py`'s
stateful `FakeJiraInstance` — a real HTTP round trip per Jira call, whose
issues remember their status and fields. A Jira-side change reaches `core`
here the way it does in production: as the webhook Jira would send for it.
"""

from __future__ import annotations

import json
import uuid

import pytest
from django.core.management import call_command
from django.test import Client

from workitems import command_consumer, jira_writer, project_config, registry, store, write_gate
from workitems.envelope import Kind, build_envelope
from workitems.models import (
    JiraWriteCompletion, OutboxEvent, WebhookFailure, WorkItem, WorkItemComment, WorkItemHistory,
    WorkItemReleaseDetail,
)
from workitems.relay import relay_once
from workitems.stream_topology import event_stream_name
from workitems.webhook_consumer import handle_webhook_envelope

from tests.jira_fixture import jira_instance, permissive_jira  # noqa: F401 - pytest fixtures, used by name
from tests.jira_fixture import posted_comment_texts, requests_to

PROJECT = 'test-project'
STORY_DETAIL = {'behavior': 'b', 'acceptanceCriteria': 'ac', 'constraints': 'c', 'edgeCases': 'e',
                'outOfScope': 'oos'}

FIELD_IDS = {
    'JIRA_TARGET_PROJECT_FIELD_ID': 'cf_target',
    'JIRA_RELEASE_NOTES_FIELD_ID': 'cf_notes',
    'JIRA_CANDIDATE_SHA_FIELD_ID': 'cf_sha',
    'JIRA_BUILD_IDENTIFIER_FIELD_ID': 'cf_build',
    'JIRA_PREVIEW_URL_FIELD_ID': 'cf_preview',
}

REJECTION_END = 'Resolve this and request the release again.'


@pytest.fixture
def release_fields(monkeypatch):
    for name, field_id in FIELD_IDS.items():
        monkeypatch.setenv(name, field_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def release_events(work_item_id=None):
    rows = OutboxEvent.objects.filter(event_type=store.RELEASE_EVENT_TYPE).order_by('created_at')
    if work_item_id is not None:
        rows = rows.filter(work_item_id=work_item_id)
    return list(rows)


def kinds(work_item_id=None):
    return [row.payload['kind'] for row in release_events(work_item_id)]


def candidate_events(work_item_id):
    return list(OutboxEvent.objects.filter(event_type='work_item.release_candidate_recorded',
                                           work_item_id=work_item_id))


def status_sequence(work_item_id):
    return [row.new_value for row in WorkItemHistory.objects.filter(
        work_item_id=work_item_id, field='status').order_by('occurred_at', 'id')]


def make_item(*, item_type='task', status='proposed', display_name='An item', external_key=None,
              project=PROJECT):
    item_id = uuid.uuid4()
    store.create_work_item({
        'id': item_id, 'project': project, 'type': item_type, 'displayName': display_name,
        'status': status, 'externalKey': external_key,
        'storyDetail': STORY_DETAIL if item_type == 'story' else None,
    })
    return item_id


def command(payload):
    return build_envelope(Kind.WORK_ITEM_COMMAND, PROJECT, payload=payload)


def request_release_locally(release_id):
    """A local release request: the `transitionStatus` command to
    `in-progress`, through the Streams command handler (REQ-09)."""
    return command_consumer.handle_command(command({
        'command': 'transitionStatus', 'actor': 'scrummaster',
        'workItemId': str(release_id), 'status': 'in-progress',
    }))


def report(release_id, **body):
    """The release-candidate job's report, through the real endpoint."""
    return Client().post(f'/admin/work-items/{release_id}/release-candidate', data=json.dumps(body),
                         content_type='application/json')


def move_back_to_in_progress(release_id):
    """An `in-review` Release moved back to `in-progress` through the external
    API's transition view, so a new candidate can be reported (REQ-09)."""
    response = Client().post(f'/admin/work-items/{release_id}/transition',
                             data=json.dumps({'status': 'in-progress'}), content_type='application/json')
    assert response.status_code == 200, response.content


def record_in_progress(instance, key):
    """The Release ticket's In Progress webhook, which records `in-progress`
    in `core` — the only status a report is accepted for (REQ-09)."""
    handle_webhook_envelope(status_webhook(instance, key, 'In Progress', 'Backlog'))
    assert WorkItem.objects.get(external_key=key).status == 'in-progress'


def webhook(instance, key, event='jira:issue_updated', *, changelog=None, message_id=None):
    body = instance.issue_webhook(key, event, changelog=changelog)
    return build_envelope(Kind.WEBHOOK_EVENT, registry.normalize_project_name(PROJECT), payload={
        'event': event, 'issue': body['issue'], 'body': body,
    }, message_id=message_id)


def status_webhook(instance, key, to_status, from_status=None):
    return webhook(instance, key, changelog=[
        {'field': 'status', 'fromString': from_status, 'toString': to_status}])


def jira_release(instance, key='TP-50', *, status='Backlog', target_project=PROJECT, candidate_sha=None):
    fields = {}
    if target_project is not None:
        fields['cf_target'] = {'key': 'TP', 'name': target_project}
    if candidate_sha is not None:
        fields['cf_sha'] = candidate_sha
    instance.add_issue(key, issuetype='Release', status=status, fields=fields)
    return key


def run_writer(redis_client):
    """Relay every outbox row onto its stream and feed each work-item event to
    the writer, as the writer's consumer group would."""
    while relay_once(redis_client):
        pass
    for project in {row.project for row in OutboxEvent.objects.all()}:
        for _, fields in redis_client.xrange(event_stream_name(project), '-', '+'):
            jira_writer.handle_event_envelope(json.loads(fields['data']))


def in_progress_pushes(key):
    return [r for r in requests_to('POST', f'/issue/{key}/transitions')]


# ---------------------------------------------------------------------------
# REQ-09 — one status sequence, in both modes
# ---------------------------------------------------------------------------

def test_local_a_requested_release_records_proposed_in_progress_in_review_in_order(clean_db):
    release_id = make_item(item_type='release', display_name='R1')

    request_release_locally(release_id)
    assert store.get_work_item(release_id).status == 'in-progress', 'no in-review before the candidate'

    response = report(release_id, candidateSha='abc1234', buildIdentifier='b-1',
                      previewUrl='https://preview.example/abc1234')
    assert response.status_code == 200, response.content

    assert status_sequence(release_id) == ['proposed', 'in-progress', 'in-review']
    assert kinds(release_id) == ['requested'], 'exactly one `requested`; the candidate publishes no release event'
    in_review = OutboxEvent.objects.get(event_type='work_item.status_changed', work_item_id=release_id,
                                         payload__status='in-review')
    assert in_review.payload['origin'] == write_gate.Origins.EXTERNAL_API


def test_jira_a_requested_release_records_proposed_in_progress_in_review_in_order(
        clean_db, release_fields, jira_instance, redis_client):  # noqa: F811
    project_config.set_mode(PROJECT, 'jira')
    key = jira_release(jira_instance)

    handle_webhook_envelope(webhook(jira_instance, key, 'jira:issue_created'))
    item = WorkItem.objects.get(external_key=key)
    assert jira_instance.status_of(key) == 'In Progress', 'the In Progress push is made after the request'
    assert item.status == 'proposed', 'core records nothing until the webhook'

    handle_webhook_envelope(status_webhook(jira_instance, key, 'In Progress', 'Backlog'))
    item.refresh_from_db()
    assert item.status == 'in-progress'
    assert kinds(item.id) == ['requested'], 'the In Progress echo publishes nothing'

    assert report(item.id, candidateSha='abc1234', buildIdentifier='b-1').status_code == 202
    assert jira_instance.status_of(key) == 'In Progress', 'the ticket shows In Progress until the candidate'
    item.refresh_from_db()
    assert item.status == 'in-progress', 'neither mode records in-review before the candidate'

    handle_webhook_envelope(webhook(jira_instance, key, changelog=[
        {'field': 'Candidate SHA', 'fieldId': 'cf_sha', 'toString': 'abc1234'}]))
    run_writer(redis_client)
    assert jira_instance.status_of(key) == 'In Review'
    handle_webhook_envelope(status_webhook(jira_instance, key, 'In Review', 'In Progress'))

    assert status_sequence(item.id) == ['proposed', 'in-progress', 'in-review']
    assert kinds(item.id) == ['requested']
    assert not WebhookFailure.objects.exists()


def test_jira_a_redelivered_creation_webhook_makes_no_second_in_progress_push(
        clean_db, release_fields, jira_instance):  # noqa: F811
    project_config.set_mode(PROJECT, 'jira')
    key = jira_release(jira_instance)
    envelope = webhook(jira_instance, key, 'jira:issue_created')

    handle_webhook_envelope(envelope)
    handle_webhook_envelope(envelope)

    item = WorkItem.objects.get(external_key=key)
    assert len(in_progress_pushes(key)) == 1
    assert JiraWriteCompletion.objects.get(
        completion_key=f'{envelope["messageId"]}:release-in-progress', work_item_id=item.id,
    ).completed_at is not None
    assert kinds() == ['requested'], 'a redelivered creation webhook publishes nothing'


def test_jira_a_creation_webhook_redelivered_after_its_push_failed_makes_the_push(
        clean_db, release_fields, jira_instance):  # noqa: F811
    project_config.set_mode(PROJECT, 'jira')
    key = jira_release(jira_instance)
    envelope = webhook(jira_instance, key, 'jira:issue_created')
    route = ('POST', f'/rest/api/3/issue/{key}/transitions')
    working = jira_instance.handler.routes[route]
    jira_instance.handler.routes[route] = lambda q, b: (503, {'errorMessages': ['unavailable']})

    with pytest.raises(Exception):
        handle_webhook_envelope(envelope)
    assert jira_instance.status_of(key) == 'Backlog'
    assert kinds() == ['requested'], 'the Release and its event committed before the push'

    jira_instance.handler.routes[route] = working
    handle_webhook_envelope(envelope)

    assert jira_instance.status_of(key) == 'In Progress'
    assert kinds() == ['requested']


def test_jira_a_refused_request_redelivered_after_its_queue_clears_is_not_pushed(
        clean_db, release_fields, jira_instance):  # noqa: F811
    outstanding = make_item(item_type='story', status='in-review', display_name='Awaiting', external_key='TP-2')
    project_config.set_mode(PROJECT, 'jira')
    key = jira_release(jira_instance)
    envelope = webhook(jira_instance, key, 'jira:issue_created')

    handle_webhook_envelope(envelope)
    item = WorkItem.objects.get(external_key=key)
    assert kinds() == [] and item.status == 'proposed'

    WorkItem.objects.filter(id=outstanding).update(status='done')
    handle_webhook_envelope(envelope)

    assert in_progress_pushes(key) == []
    assert jira_instance.status_of(key) == 'Backlog'
    assert kinds() == [], 'and still no `requested`'
    assert len(posted_comment_texts()) == 1, 'the one rejection, posted once'


def test_jira_a_creation_webhook_replayed_after_the_release_reaches_in_review_makes_no_push(
        clean_db, release_fields, jira_instance):  # noqa: F811
    """The push failed on the first delivery, so the step is still pending
    when the dead-lettered creation webhook is replayed — after the Release
    has reached `in-review` (`streams.py`'s `replay`)."""
    project_config.set_mode(PROJECT, 'jira')
    key = jira_release(jira_instance)
    envelope = webhook(jira_instance, key, 'jira:issue_created')
    route = ('POST', f'/rest/api/3/issue/{key}/transitions')
    working = jira_instance.handler.routes[route]
    jira_instance.handler.routes[route] = lambda q, b: (503, {'errorMessages': ['unavailable']})
    with pytest.raises(Exception):
        handle_webhook_envelope(envelope)
    jira_instance.handler.routes[route] = working

    item = WorkItem.objects.get(external_key=key)
    jira_instance.issues[key]['status'] = 'In Review'  # a person moved it, its candidate in hand
    handle_webhook_envelope(status_webhook(jira_instance, key, 'In Review', 'Backlog'))
    item.refresh_from_db()
    assert item.status == 'in-review'

    handle_webhook_envelope(envelope)

    assert jira_instance.status_of(key) == 'In Review', 'never moved back to In Progress'
    assert JiraWriteCompletion.objects.get(
        completion_key=f'{envelope["messageId"]}:release-in-progress', work_item_id=item.id,
    ).completed_at is not None, 'recorded complete without a push'


# ---------------------------------------------------------------------------
# REQ-09 — a report only for an `in-progress` Release, in both modes
# ---------------------------------------------------------------------------

LATE = dict(candidateSha='late999', buildIdentifier='b-late', previewUrl='https://preview.example/late999',
            nativeBuildUrl='https://ci.example/native/99', nativeBuildStatus='success')


def local_snapshot(release_id):
    """Everything a report could change in `core`: the Release's status and
    status history, its candidate fields, its comments, and every outbox
    row (status, candidate, release and comment events alike)."""
    detail = WorkItemReleaseDetail.objects.filter(work_item_id=release_id).first()
    return {
        'status': store.get_work_item(release_id).status,
        'history': status_sequence(release_id),
        'candidate': detail and (detail.candidate_sha, detail.build_identifier, detail.preview_url),
        'comments': sorted(WorkItemComment.objects.filter(work_item_id=release_id).values_list('body', flat=True)),
        'outbox': sorted(OutboxEvent.objects.values_list('id', flat=True)),
    }


def assert_refused(response):
    assert 400 <= response.status_code < 500, (response.status_code, response.content)
    assert response.json()['error'] == 'VALIDATION_ERROR'
    assert '"in-progress"' in response.json()['message']


def local_release_at(status):
    """A local Release brought to `status` through the production paths: the
    Streams command handler and the report endpoint."""
    release_id = make_item(item_type='release')
    if status == 'proposed':
        return release_id
    request_release_locally(release_id)
    if status in ('in-review', 'done'):
        assert report(release_id, candidateSha='abc1234').status_code == 200
    if status in ('done', 'cancelled'):
        command_consumer.handle_command(command({
            'command': 'transitionStatus', 'actor': 'scrummaster',
            'workItemId': str(release_id), 'status': status}))
    assert store.get_work_item(release_id).status == status
    return release_id


@pytest.mark.parametrize('status', ['proposed', 'in-review', 'done', 'cancelled'])
def test_local_a_report_for_a_release_not_in_progress_is_refused_and_changes_nothing(clean_db, status):
    release_id = local_release_at(status)
    before = local_snapshot(release_id)

    assert_refused(report(release_id, **LATE))

    assert local_snapshot(release_id) == before, 'no status change, no candidate, no comment, no event'
    if status == 'done':
        assert kinds(release_id) == ['requested', 'done'], 'a late report never leads to a second `done`'


def test_local_an_in_review_release_moved_back_to_in_progress_takes_a_new_candidate_and_cuts_none(clean_db):
    release_id = local_release_at('in-review')

    move_back_to_in_progress(release_id)
    assert kinds(release_id) == ['requested'], 'the move back publishes no release event (REQ-10)'
    assert report(release_id, candidateSha='def5678').status_code == 200

    assert store.get_work_item(release_id).status == 'in-review'
    assert WorkItemReleaseDetail.objects.get(work_item_id=release_id).candidate_sha == 'def5678'
    assert [e.payload['candidateSha'] for e in candidate_events(release_id)] == ['abc1234', 'def5678']
    assert kinds(release_id) == ['requested']


JIRA_STATUS = {'in-review': 'In Review', 'done': 'Done', 'cancelled': 'Abandoned'}


def jira_snapshot(instance, key, release_id):
    """`core`'s side plus Jira's: every request the fake Jira has received
    (field pushes, comment posts, transitions) and the ticket's own state."""
    return {
        **local_snapshot(release_id),
        'jira_requests': len(instance.handler.received),
        'jira_status': instance.status_of(key),
        'jira_fields': dict(instance.issues[key]['fields']),
        'completions': JiraWriteCompletion.objects.count(),
        'failures': WebhookFailure.objects.count(),
    }


@pytest.mark.parametrize('status', ['proposed', 'in-review', 'done', 'cancelled'])
def test_jira_a_report_for_a_release_not_in_progress_is_refused_and_makes_no_jira_write(
        clean_db, release_fields, jira_instance, status):  # noqa: F811
    project_config.set_mode(PROJECT, 'jira', jira_project_key='TP')
    # Created in Backlog, the request pushes In Progress; `core` holds
    # `proposed` until the In Progress webhook, then the ticket moves on.
    key = jira_release(jira_instance)
    handle_webhook_envelope(webhook(jira_instance, key, 'jira:issue_created'))
    item = WorkItem.objects.get(external_key=key)
    if status != 'proposed':
        record_in_progress(jira_instance, key)
        jira_instance.issues[key]['status'] = JIRA_STATUS[status]
        handle_webhook_envelope(status_webhook(jira_instance, key, JIRA_STATUS[status], 'In Progress'))
    item.refresh_from_db()
    assert item.status == status
    before = jira_snapshot(jira_instance, key, item.id)

    assert_refused(report(item.id, **LATE))

    assert jira_snapshot(jira_instance, key, item.id) == before, \
        'no status change, no candidate, no comment, no event and no Jira write'
    assert requests_to('PUT', f'/issue/{key}') == [], 'no field push'
    assert posted_comment_texts() == [], 'no comment posted'


def test_jira_a_report_for_an_in_progress_release_is_still_pushed(
        clean_db, release_fields, jira_instance):  # noqa: F811
    project_config.set_mode(PROJECT, 'jira', jira_project_key='TP')
    key = jira_release(jira_instance, status='In Progress')
    handle_webhook_envelope(webhook(jira_instance, key, 'jira:issue_created'))
    record_in_progress(jira_instance, key)
    item = WorkItem.objects.get(external_key=key)

    assert report(item.id, **LATE).status_code == 202

    assert jira_instance.issues[key]['fields']['cf_sha'] == 'late999'
    assert posted_comment_texts() == ['[jenkins] Native build (success): https://ci.example/native/99']


# ---------------------------------------------------------------------------
# REQ-10 — one release-event publisher, two callers
# ---------------------------------------------------------------------------

def test_both_modes_events_carry_the_same_three_payload_keys_on_their_callers_stream(
        clean_db, release_fields, jira_instance):  # noqa: F811
    local_release = make_item(item_type='release', project='engineering-app')
    command_consumer.handle_command(build_envelope(Kind.WORK_ITEM_COMMAND, 'engineering-app', payload={
        'command': 'transitionStatus', 'actor': 'scrummaster',
        'workItemId': str(local_release), 'status': 'in-progress'}))

    project_config.set_mode(PROJECT, 'jira')
    project_config.set_mode('jira-target', 'jira')
    key = jira_release(jira_instance, target_project='jira-target')
    handle_webhook_envelope(webhook(jira_instance, key, 'jira:issue_created'))
    jira_item = WorkItem.objects.get(external_key=key)

    [local_row] = release_events(local_release)
    [jira_row] = release_events(jira_item.id)
    assert set(local_row.payload) == set(jira_row.payload) == {'kind', 'workItemId', 'project'}
    assert local_row.project == 'engineering-app' and local_row.payload['project'] == 'engineering-app'
    assert jira_row.project == PROJECT, "a Jira-mode event is on the ticket's own project's stream"
    assert jira_row.payload['project'] == 'jira-target', 'with its Target Project as the payload project'


def test_jira_done_reopened_done_publishes_done_twice_and_a_no_change_webhook_publishes_nothing(
        clean_db, release_fields, jira_instance):  # noqa: F811
    project_config.set_mode(PROJECT, 'jira')
    key = jira_release(jira_instance)
    handle_webhook_envelope(webhook(jira_instance, key, 'jira:issue_created'))
    item = WorkItem.objects.get(external_key=key)

    handle_webhook_envelope(status_webhook(jira_instance, key, 'Done', 'In Review'))
    handle_webhook_envelope(status_webhook(jira_instance, key, 'Done', 'Done'))  # changes nothing
    handle_webhook_envelope(status_webhook(jira_instance, key, 'In Progress', 'Done'))
    handle_webhook_envelope(status_webhook(jira_instance, key, 'Done', 'In Progress'))

    assert kinds(item.id) == ['requested', 'done', 'done']
    item.refresh_from_db()
    assert item.status == 'done'


def test_local_a_release_moved_back_from_in_review_to_in_progress_publishes_nothing(clean_db):
    release_id = make_item(item_type='release')
    request_release_locally(release_id)
    store.transition_status(release_id, 'in-review', actor='tester')

    store.transition_status(release_id, 'in-progress', actor='tester')

    assert kinds(release_id) == ['requested'], 'cuts no second candidate'


def test_jira_a_failure_between_done_and_its_event_leaves_neither_and_redelivery_publishes_once(
        clean_db, release_fields, jira_instance, monkeypatch):  # noqa: F811
    project_config.set_mode(PROJECT, 'jira')
    key = jira_release(jira_instance)
    handle_webhook_envelope(webhook(jira_instance, key, 'jira:issue_created'))
    item = WorkItem.objects.get(external_key=key)
    done = status_webhook(jira_instance, key, 'Done', 'In Progress')

    real_publish = store.publish_release_event

    def crash(*args, **kwargs):
        raise RuntimeError('injected crash after the status, before the event')

    monkeypatch.setattr(store, 'publish_release_event', crash)
    with pytest.raises(RuntimeError):
        handle_webhook_envelope(done)
    item.refresh_from_db()
    assert item.status == 'proposed', 'the status rolled back with the event'
    assert kinds(item.id) == ['requested']

    monkeypatch.setattr(store, 'publish_release_event', real_publish)
    handle_webhook_envelope(done)

    item.refresh_from_db()
    assert item.status == 'done'
    assert kinds(item.id) == ['requested', 'done']


# ---------------------------------------------------------------------------
# REQ-11 — one release gate and one rejection format
# ---------------------------------------------------------------------------

def _beta_rejection_body(outstanding_name, outstanding_ref):
    return ('[system] release request rejected: 1 work item(s) are still awaiting acceptance on beta\n\n'
            f'  - {outstanding_name} ({outstanding_ref})\n\n' + REJECTION_END)


def test_local_a_request_with_a_story_awaiting_acceptance_leaves_one_comment_and_raises(clean_db):
    outstanding = make_item(item_type='story', status='in-review', display_name='Awaiting')
    release_id = make_item(item_type='release')
    envelope = command({'command': 'transitionStatus', 'actor': 'scrummaster',
                         'workItemId': str(release_id), 'status': 'in-progress'})

    for _ in range(2):  # the second is a redelivery with the same completion key
        with pytest.raises(store.ReleaseGateError):
            command_consumer._handler(envelope)

    [comment] = WorkItemComment.objects.filter(work_item_id=release_id)
    assert comment.author == 'system'
    assert comment.body == _beta_rejection_body('Awaiting', outstanding)
    assert store.get_work_item(release_id).status == 'proposed', 'the in-progress move is not recorded'
    assert status_sequence(release_id) == ['proposed']
    assert kinds(release_id) == []


def test_jira_both_refusals_leave_one_comment_in_the_same_format_as_local_mode(
        clean_db, release_fields, jira_instance):  # noqa: F811
    make_item(item_type='story', status='in-review', display_name='Awaiting', external_key='TP-2')
    project_config.set_mode(PROJECT, 'jira')
    no_target = jira_release(jira_instance, 'TP-60', target_project=None)
    queued = jira_release(jira_instance, 'TP-61')

    handle_webhook_envelope(webhook(jira_instance, no_target, 'jira:issue_created'))
    handle_webhook_envelope(webhook(jira_instance, queued, 'jira:issue_created'))

    assert posted_comment_texts() == [
        '[system] [system] release request rejected: Target Project is not set\n\n' + REJECTION_END,
        '[system] ' + _beta_rejection_body('Awaiting', 'TP-2'),
    ], 'the writer\'s "[<author>] " prefix over the one rejection text'
    fallback = WorkItem.objects.get(external_key=no_target)
    assert fallback.project == PROJECT, 'item.project holds the fallback; the gate still refuses'
    for key in (no_target, queued):
        item = WorkItem.objects.get(external_key=key)
        assert item.status == 'proposed'
        assert status_sequence(item.id) == ['proposed'], 'no status change recorded'
    assert kinds() == [], 'no `requested`'
    assert requests_to('POST', '/issue/TP-60/transitions') == []
    assert requests_to('POST', '/issue/TP-61/transitions') == []


def test_jira_a_rejection_whose_post_failed_is_posted_on_redelivery_once(
        clean_db, release_fields, jira_instance):  # noqa: F811
    project_config.set_mode(PROJECT, 'jira')
    key = jira_release(jira_instance, target_project=None)
    envelope = webhook(jira_instance, key, 'jira:issue_created')
    route = ('POST', f'/rest/api/3/issue/{key}/comment')
    working = jira_instance.handler.routes[route]
    jira_instance.handler.routes[route] = lambda q, b: (503, {'errorMessages': ['unavailable']})

    with pytest.raises(Exception):
        handle_webhook_envelope(envelope)
    item = WorkItem.objects.get(external_key=key)
    assert item.status == 'proposed' and kinds() == [], 'the refused Release committed with no `requested`'

    attempts = len(requests_to('POST', f'/issue/{key}/comment'))
    assert attempts == 1, 'the one failed attempt'

    jira_instance.handler.routes[route] = working
    handle_webhook_envelope(envelope)
    handle_webhook_envelope(envelope)

    assert len(requests_to('POST', f'/issue/{key}/comment')) == attempts + 1, \
        'posted on the first redelivery, and not again on the second'
    assert kinds() == []


def test_jira_a_requested_releases_creation_redelivered_after_a_story_enters_review_gets_no_rejection(
        clean_db, release_fields, jira_instance):  # noqa: F811
    story = make_item(item_type='story', status='in-progress', display_name='Story', external_key='TP-2')
    project_config.set_mode(PROJECT, 'jira')
    key = jira_release(jira_instance)
    envelope = webhook(jira_instance, key, 'jira:issue_created')
    handle_webhook_envelope(envelope)

    WorkItem.objects.filter(id=story).update(status='in-review')
    handle_webhook_envelope(envelope)

    assert posted_comment_texts() == []
    assert kinds() == ['requested']


# ---------------------------------------------------------------------------
# REQ-12 — the candidate note and the native-build comment
# ---------------------------------------------------------------------------

NOTE = ('Release candidate cut: abc1234 (build b-7)\n'
        'Preview: https://preview.example/abc1234\n'
        'Release PR: release/abc1234 → prod')
NATIVE = 'Native build (success): https://ci.example/native/12'


def test_local_a_report_with_a_native_build_leaves_one_note_and_one_native_build_comment(clean_db):
    release_id = make_item(item_type='release')
    request_release_locally(release_id)
    body = dict(candidateSha='abc1234', buildIdentifier='b-7', previewUrl='https://preview.example/abc1234',
                nativeBuildUrl='https://ci.example/native/12', nativeBuildStatus='success')

    assert report(release_id, **body).status_code == 200
    move_back_to_in_progress(release_id)  # a report is accepted only at `in-progress` (REQ-09)
    assert report(release_id, **body).status_code == 200  # a repeated report, same SHA

    bodies = [c.body for c in WorkItemComment.objects.filter(work_item_id=release_id)]
    assert bodies.count(NATIVE) == 1, 'a repeated report posts the native-build comment once'
    assert NOTE in bodies
    native = WorkItemComment.objects.get(body=NATIVE)
    assert native.source_message_id == 'release-candidate:abc1234:native-build'
    assert not any('Native' in name for name in
                   [f.name for f in WorkItemReleaseDetail._meta.get_fields()]), 'no native-build column'


def test_local_a_report_without_a_native_build_leaves_the_note_alone(clean_db):
    release_id = make_item(item_type='release')
    request_release_locally(release_id)

    report(release_id, candidateSha='abc1234')

    assert [c.body for c in WorkItemComment.objects.filter(work_item_id=release_id)] == [
        'Release candidate cut: abc1234\nRelease PR: release/abc1234 → prod',
    ], 'the build and preview lines appear only when their values are present'


def test_jira_a_report_with_no_candidate_field_id_configured_fails_the_job_and_records_nothing(
        clean_db, jira_instance, monkeypatch):  # noqa: F811
    """REQ-13, "The report": a failed push returns the error to the job and
    nothing is recorded. A push that would write nothing because none of the
    three candidate field ids is configured is a failed push: an error
    status, no native-build comment, nothing recorded, the failure kept."""
    for name, field_id in FIELD_IDS.items():
        if name in ('JIRA_CANDIDATE_SHA_FIELD_ID', 'JIRA_BUILD_IDENTIFIER_FIELD_ID', 'JIRA_PREVIEW_URL_FIELD_ID'):
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, field_id)
    project_config.set_mode(PROJECT, 'jira')
    key = jira_release(jira_instance, status='In Progress')
    handle_webhook_envelope(webhook(jira_instance, key, 'jira:issue_created'))
    record_in_progress(jira_instance, key)  # a report is accepted only at `in-progress` (REQ-09)
    release_id = WorkItem.objects.get(external_key=key).id

    response = report(release_id, candidateSha='abc1234', buildIdentifier='b-7',
                      nativeBuildUrl='https://ci.example/native/12', nativeBuildStatus='success')

    assert response.status_code == 500, 'the job sees an error, as it does for a failed set_fields'
    assert posted_comment_texts() == [], 'no native-build comment follows a failed push'
    assert requests_to('PUT', f'/issue/{key}') == [], 'nothing was written to Jira'
    assert WorkItemComment.objects.count() == 0
    assert candidate_events(release_id) == []
    assert WorkItemReleaseDetail.objects.filter(work_item_id=release_id).exclude(candidate_sha=None).count() == 0
    [failure] = WebhookFailure.objects.all()
    assert 'no release-candidate field id is configured' in failure.reason


def test_jira_the_native_build_comment_precedes_the_note_and_is_recorded_only_from_its_webhook(
        clean_db, release_fields, jira_instance, redis_client):  # noqa: F811
    project_config.set_mode(PROJECT, 'jira')
    key = jira_release(jira_instance, status='In Progress')
    handle_webhook_envelope(webhook(jira_instance, key, 'jira:issue_created'))
    record_in_progress(jira_instance, key)  # a report is accepted only at `in-progress` (REQ-09)
    item = WorkItem.objects.get(external_key=key)
    body = dict(candidateSha='abc1234', buildIdentifier='b-7', previewUrl='https://preview.example/abc1234',
                nativeBuildUrl='https://ci.example/native/12', nativeBuildStatus='success')

    assert report(item.id, **body).status_code == 202
    assert report(item.id, **body).status_code == 202  # repeated
    assert posted_comment_texts() == [f'[jenkins] {NATIVE}'], 'posted once, after the push'
    assert WorkItemComment.objects.count() == 0, 'recorded only from its webhook'

    handle_webhook_envelope(webhook(jira_instance, key, changelog=[
        {'field': 'Candidate SHA', 'fieldId': 'cf_sha', 'toString': 'abc1234'}]))

    texts = posted_comment_texts()
    assert texts == [f'[jenkins] {NATIVE}', f'[system] {NOTE}'], 'the note waits for the webhook'
    together = '\n'.join(texts)
    for value in ('abc1234', 'b-7', 'https://preview.example/abc1234', 'release/abc1234 → prod',
                  'https://ci.example/native/12', 'success'):
        assert value in together, f'the two comments carry {value}'

    # The comment reaches `core` as every comment does: its own webhook.
    comment_body = {'webhookEvent': 'comment_created', 'issue': {'key': key, 'fields': {}},
                    'comment': {'id': '9001', 'author': {'displayName': 'AI Gang',
                                                          'emailAddress': 'bot@example.com'},
                                'body': {'type': 'doc', 'version': 1, 'content': [
                                    {'type': 'paragraph', 'content': [{'type': 'text', 'text': texts[0]}]}]}}}
    handle_webhook_envelope(build_envelope(Kind.WEBHOOK_EVENT, PROJECT, payload={
        'event': 'comment_created', 'issue': comment_body['issue'], 'body': comment_body}))
    [recorded] = WorkItemComment.objects.filter(work_item_id=item.id)
    assert (recorded.author, recorded.body) == ('jenkins', NATIVE)


# ---------------------------------------------------------------------------
# REQ-13 — candidate fields pushed first in one edit, recorded from the webhook
# ---------------------------------------------------------------------------

def test_jira_the_candidate_is_recorded_from_one_webhook_and_published_once(
        clean_db, release_fields, jira_instance):  # noqa: F811
    project_config.set_mode(PROJECT, 'jira')
    key = jira_release(jira_instance, status='In Progress')
    handle_webhook_envelope(webhook(jira_instance, key, 'jira:issue_created'))
    record_in_progress(jira_instance, key)  # a report is accepted only at `in-progress` (REQ-09)
    item = WorkItem.objects.get(external_key=key)

    report(item.id, candidateSha='abc1234', buildIdentifier='b-7', previewUrl='https://preview.example/abc1234')
    assert len(requests_to('PUT', f'/issue/{key}')) == 1, 'one Jira edit'
    assert candidate_events(item.id) == [], 'the report records nothing'

    edit = webhook(jira_instance, key, changelog=[
        {'field': 'Candidate SHA', 'fieldId': 'cf_sha', 'toString': 'abc1234'},
        {'field': 'Build Identifier', 'fieldId': 'cf_build', 'toString': 'b-7'},
        {'field': 'Preview URL', 'fieldId': 'cf_preview', 'toString': 'https://preview.example/abc1234'},
    ])
    handle_webhook_envelope(edit)
    handle_webhook_envelope(edit)  # redelivered

    [event] = candidate_events(item.id)
    assert event.payload == {'id': str(item.id), 'candidateSha': 'abc1234', 'buildIdentifier': 'b-7',
                             'previewUrl': 'https://preview.example/abc1234'}
    detail = WorkItemReleaseDetail.objects.get(work_item_id=item.id)
    jira_fields = jira_instance.issues[key]['fields']
    assert (detail.candidate_sha, detail.build_identifier, detail.preview_url) == \
        (jira_fields['cf_sha'], jira_fields['cf_build'], jira_fields['cf_preview']), "core's and Jira's are equal"

    # A person edits the SHA in Jira: a new candidate.
    jira_instance.issues[key]['fields']['cf_sha'] = 'def5678'
    handle_webhook_envelope(webhook(jira_instance, key, changelog=[
        {'field': 'Candidate SHA', 'fieldId': 'cf_sha', 'toString': 'def5678'}]))
    assert [e.payload['candidateSha'] for e in candidate_events(item.id)] == ['abc1234', 'def5678']


def test_jira_a_release_created_with_a_candidate_sha_publishes_no_candidate_event(
        clean_db, release_fields, jira_instance):  # noqa: F811
    project_config.set_mode(PROJECT, 'jira')
    key = jira_release(jira_instance, candidate_sha='cloned1')

    handle_webhook_envelope(webhook(jira_instance, key, 'jira:issue_created'))
    handle_webhook_envelope(status_webhook(jira_instance, key, 'In Progress', 'Backlog'))

    item = WorkItem.objects.get(external_key=key)
    assert WorkItemReleaseDetail.objects.get(work_item_id=item.id).candidate_sha == 'cloned1', 'recorded as a field'
    assert candidate_events(item.id) == []


def test_local_a_report_records_and_publishes_as_today(clean_db):
    release_id = make_item(item_type='release')
    request_release_locally(release_id)

    report(release_id, candidateSha='abc1234', buildIdentifier='b-7')

    [event] = candidate_events(release_id)
    assert event.payload['candidateSha'] == 'abc1234'
    assert WorkItemReleaseDetail.objects.get(work_item_id=release_id).candidate_sha == 'abc1234'


# ---------------------------------------------------------------------------
# REQ-14 — Abandoned
# ---------------------------------------------------------------------------

def test_jira_a_release_moved_to_abandoned_publishes_abandoned_once_and_disconnect_no_longer_refuses(
        clean_db, release_fields, jira_instance):  # noqa: F811
    project_config.set_mode(PROJECT, 'jira', jira_project_key='TP')
    key = jira_release(jira_instance)
    handle_webhook_envelope(webhook(jira_instance, key, 'jira:issue_created'))
    item = WorkItem.objects.get(external_key=key)

    abandon = status_webhook(jira_instance, key, 'Abandoned', 'In Progress')
    handle_webhook_envelope(abandon)
    handle_webhook_envelope(abandon)

    item.refresh_from_db()
    assert item.status == 'cancelled'
    assert kinds(item.id) == ['requested', 'abandoned'], 'exactly one `abandoned` and no `done` (no promotion)'

    call_command('disconnect_jira', PROJECT, stdout=__import__('io').StringIO())
    assert project_config.get_mode(PROJECT)['mode'] == 'local'


def test_jira_a_release_cancelled_in_core_shows_abandoned_in_jira(clean_db, jira_instance):  # noqa: F811
    jira_instance.add_issue('TP-70', issuetype='Release', status='In Progress')
    release_id = make_item(item_type='release', status='in-progress', external_key='TP-70')
    project_config.set_mode(PROJECT, 'jira')

    command_consumer.handle_command(command({
        'command': 'transitionStatus', 'actor': 'scrummaster',
        'workItemId': str(release_id), 'status': 'cancelled'}))

    assert jira_instance.status_of('TP-70') == 'Abandoned'
    assert jira_instance.blocked('TP-70') is None, 'not the Blocked flag'
    assert store.get_work_item(release_id).status == 'in-progress', 'recorded only from the webhook'


def test_a_story_cancelled_in_core_shows_abandoned_and_one_moved_to_abandoned_is_recorded_cancelled(
        clean_db, jira_instance):  # noqa: F811
    jira_instance.add_issue('TP-71', issuetype='Story', status='In Progress')
    jira_instance.add_issue('TP-72', issuetype='Story', status='In Progress')
    pushed = make_item(item_type='story', status='in-progress', external_key='TP-71')
    moved = make_item(item_type='story', status='in-progress', external_key='TP-72')
    project_config.set_mode(PROJECT, 'jira')

    store.transition_status(pushed, 'cancelled', actor='scrummaster', origin=write_gate.Origins.DIRECT)
    jira_instance.issues['TP-72']['status'] = 'Abandoned'
    handle_webhook_envelope(status_webhook(jira_instance, 'TP-72', 'Abandoned', 'In Progress'))

    assert jira_instance.status_of('TP-71') == 'Abandoned'
    assert jira_instance.blocked('TP-71') is None
    assert store.get_work_item(moved).status == 'cancelled'


def test_a_done_write_on_a_story_in_abandoned_records_one_failure_and_changes_nothing(
        clean_db, jira_instance):  # noqa: F811
    jira_instance.add_issue('TP-73', issuetype='Story', status='Abandoned')
    story = make_item(item_type='story', status='cancelled', external_key='TP-73')
    project_config.set_mode(PROJECT, 'jira')

    store.transition_status(story, 'done', actor='scrummaster', origin=write_gate.Origins.DIRECT)

    assert jira_instance.status_of('TP-73') == 'Abandoned'
    assert store.get_work_item(story).status == 'cancelled'
    [failure] = WebhookFailure.objects.all()
    assert 'offers no transition to "Done"' in failure.reason


def test_disconnect_local_cancel_connect_shows_abandoned_and_a_resynced_done_publishes_nothing(
        clean_db, release_fields, jira_instance, monkeypatch):  # noqa: F811
    """REQ-14's acceptance across the round trip: after `disconnect_jira`, a
    local cancel and `connect_jira`, the Release shows Abandoned and a later
    move to Done is impossible without a person first reopening it; and a
    re-synced `done` Release's webhook publishes nothing.

    Starts in Jira mode and runs the real `disconnect_jira`, which refuses
    while a Release is open, so both Releases are closed when it runs: one
    `failed` (the closed state a person can still cancel), one `done`."""
    import io

    from tests.test_connect_jira_command import register_webhook
    from workitems.management.commands.connect_jira import JIRA_FIELD_ID_VARS

    for index, name in enumerate(JIRA_FIELD_ID_VARS):
        if name not in FIELD_IDS and name not in ('JIRA_AGENT_FIELD_ID', 'JIRA_BLOCKED_FIELD_ID'):
            monkeypatch.setenv(name, f'customfield_3{index:04d}')
    register_webhook(jira_instance)
    jira_instance.add_issue('TP-80', issuetype='Release', status='In Review')
    jira_instance.add_issue('TP-81', issuetype='Release', status='Done')
    abandoned = make_item(item_type='release', status='failed', external_key='TP-80')
    shipped = make_item(item_type='release', status='done', external_key='TP-81')
    project_config.set_mode(PROJECT, 'jira', jira_project_key='TP')

    call_command('disconnect_jira', PROJECT, stdout=io.StringIO())
    assert project_config.get_mode(PROJECT)['mode'] == 'local'

    # Cancelled in core while the project is local.
    store.transition_status(abandoned, 'cancelled', actor='an-operator')
    assert store.get_work_item(abandoned).status == 'cancelled'
    assert jira_instance.status_of('TP-80') == 'In Review', 'a local cancel writes nothing to Jira'
    events_before = kinds()

    call_command('connect_jira', PROJECT, 'TP', stdout=io.StringIO())

    assert project_config.get_mode(PROJECT)['mode'] == 'jira'
    assert jira_instance.status_of('TP-80') == 'Abandoned'
    assert jira_instance.status_of('TP-81') == 'Done'
    assert 'Done' not in [name for _, name in jira_instance._offered_now('TP-80')], \
        'Done is not offered from Abandoned until a person moves the issue out'

    handle_webhook_envelope(status_webhook(jira_instance, 'TP-81', 'Done', 'In Review'))
    handle_webhook_envelope(status_webhook(jira_instance, 'TP-80', 'Abandoned', 'In Review'))
    assert kinds() == events_before, 'the re-sync echoes publish nothing — nothing is promoted twice'
