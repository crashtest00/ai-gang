"""
canonical-delivery-state.md REQ-04 and REQ-05 — a delivered work item
reaching `in-review` on beta delivery, in both modes, evidence before the
transition.

Driven through `command_consumer.handle_command` with a `recordBetaDeployment`
envelope — the real dispatch path the gateway's relay reaches — never by
calling `beta_deployment.apply_beta_deployment_to_work_item` directly, because
what REQ-04/REQ-05 claim is that the gateway-relayed command does these
things (Gate 1).
"""

from __future__ import annotations

import uuid

import pytest
from django.db import connection

from workitems import jira_writer, project_config, store, write_gate
from workitems.command_consumer import handle_command
from workitems.envelope import Kind, build_envelope
from workitems.models import WebhookFailure, WorkItemComment

from tests.jira_fixture import fixture_jira, permissive_jira  # noqa: F401 - pytest fixtures, used by name
from tests.jira_fixture import FixtureJiraHandler, posted_comment_texts, requests_to

PROJECT = 'test-project'

PR_URL = 'https://github.com/crashtest00/ai-gang/pull/231'


def make_item(*, status='proposed', item_type='task', external_key=None, project=PROJECT,
              display_name='A delivered task'):
    item_id = uuid.uuid4()
    store.create_work_item({
        'id': item_id, 'project': project, 'type': item_type, 'displayName': display_name,
        'status': status, 'externalKey': external_key,
    })
    return item_id


def jira_mode(project=PROJECT):
    project_config.set_mode(project, 'jira')


def offer_transition(fixture, key, *status_names):
    fixture.routes[('GET', f'/rest/api/3/issue/{key}/transitions')] = lambda q, b: (
        200, {'transitions': [{'id': str(10 + i), 'to': {'name': name}} for i, name in enumerate(status_names)]}
    )
    posted = []
    fixture.routes[('POST', f'/rest/api/3/issue/{key}/transitions')] = \
        lambda q, b: (posted.append(b), (200, None))[1]
    return posted


def allow_comment(fixture, key):
    fixture.routes[('POST', f'/rest/api/3/issue/{key}/comment')] = lambda q, b: (201, {'id': '999'})


def command(payload, project=PROJECT):
    return build_envelope(Kind.WORK_ITEM_COMMAND, project, payload=payload)


def _beta_deployed(source_message_id, *, pr_url=PR_URL, sha='deadbeef', build='hello-world-deadbee-7'):
    return {
        'command': 'recordBetaDeployment', 'sourceMessageId': source_message_id,
        'type': 'beta_deployed', 'pull_requests': [pr_url],
        'deployed_sha': sha, 'build_identifier': build,
        'build_url': 'https://ci.test/job/dev/47/', 'beta_url': 'https://hello-world.beta.example.com',
    }


# ---------------------------------------------------------------------------
# REQ-04 / REQ-05 — local mode
# ---------------------------------------------------------------------------

def test_a_delivered_work_item_reaches_in_review_in_local_mode_and_blocks_a_release(clean_db):
    item_id = make_item()
    store.attach_artifact(item_id, 'pull_request', PR_URL)

    handle_command(command(_beta_deployed('gw-20')))

    item = store.get_work_item(item_id)
    assert item.status == 'in-review'

    comment = WorkItemComment.objects.get(work_item_id=item_id)
    assert comment.author == 'jenkins'
    assert 'https://hello-world.beta.example.com' in comment.body
    assert 'deadbeef' in comment.body
    assert 'hello-world-deadbee-7' in comment.body
    assert 'https://ci.test/job/dev/47/' in comment.body
    assert comment.source_message_id == f'gw-20:{item_id}'

    release_id = make_item(item_type='release', display_name='A release')
    with pytest.raises(store.ReleaseGateError):
        store.transition_status(release_id, 'in-review', actor='a-person')


def test_a_local_mode_promotion_makes_no_outbound_call(clean_db, permissive_jira):
    """REQ-05 — "A local-mode project MUST receive no external call." The
    project is never put into Jira mode here."""
    item_id = make_item()
    store.attach_artifact(item_id, 'pull_request', PR_URL)

    handle_command(command(_beta_deployed('gw-21')))

    assert store.get_work_item(item_id).status == 'in-review'
    assert permissive_jira.received == []


def test_an_already_in_review_work_item_gets_evidence_but_no_transition_call_and_no_failure(clean_db):
    item_id = make_item(status='in-review')
    store.attach_artifact(item_id, 'pull_request', PR_URL)

    handle_command(command(_beta_deployed('gw-22')))

    assert store.get_work_item(item_id).status == 'in-review'
    assert WorkItemComment.objects.filter(work_item_id=item_id).count() == 1
    assert WebhookFailure.objects.count() == 0


@pytest.mark.parametrize('terminal_status', ['done', 'cancelled', 'failed'])
def test_a_terminal_status_work_item_gets_evidence_no_transition_and_one_webhook_failure(
        clean_db, terminal_status):
    item_id = make_item(status=terminal_status)
    pr_url = f'{PR_URL}-{terminal_status}'
    store.attach_artifact(item_id, 'pull_request', pr_url)

    handle_command(command(_beta_deployed(f'gw-term-{terminal_status}', pr_url=pr_url)))

    assert store.get_work_item(item_id).status == terminal_status
    assert WorkItemComment.objects.filter(work_item_id=item_id).count() == 1

    failures = WebhookFailure.objects.filter(work_item_id=item_id)
    assert failures.count() == 1
    failure = failures.get()
    assert str(item_id) in failure.reason
    assert failure.payload['step'] == jira_writer.STEP_STATUS


def test_a_validation_error_from_transition_status_keeps_status_and_evidence_and_records_one_failure(clean_db):
    """A Story leaving `proposed` without its required fields is
    `transition_status`'s own `ValidationError` — REQ-04 treats it exactly
    as a no-legal-transition outcome: comment kept, no status change, one
    webhook failure, nothing raised to the consumer."""
    item_id = make_item(item_type='story', display_name='A story with no detail')
    store.attach_artifact(item_id, 'pull_request', PR_URL)

    handle_command(command(_beta_deployed('gw-23')))

    assert store.get_work_item(item_id).status == 'proposed'
    assert WorkItemComment.objects.filter(work_item_id=item_id).count() == 1

    failures = WebhookFailure.objects.filter(work_item_id=item_id)
    assert failures.count() == 1
    assert failures.get().payload['step'] == jira_writer.STEP_STATUS


def test_n_resolved_work_items_each_get_their_own_evidence_and_transition(clean_db):
    first = make_item(display_name='First')
    second = make_item(display_name='Second')
    other_pr = f'{PR_URL}-other'
    store.attach_artifact(first, 'pull_request', PR_URL)
    store.attach_artifact(second, 'pull_request', other_pr)

    handle_command(command({
        **_beta_deployed('gw-24'),
        'pull_requests': [PR_URL, other_pr],
    }))

    assert store.get_work_item(first).status == 'in-review'
    assert store.get_work_item(second).status == 'in-review'
    assert WorkItemComment.objects.filter(work_item_id=first).count() == 1
    assert WorkItemComment.objects.filter(work_item_id=second).count() == 1


# ---------------------------------------------------------------------------
# REQ-04 / REQ-05 — Jira mode
# ---------------------------------------------------------------------------

def test_a_jira_mode_promotion_posts_evidence_before_the_transition_and_records_nothing_canonically(
        clean_db, fixture_jira):
    item_id = make_item(external_key='WT-1')
    jira_mode()
    store.attach_artifact(item_id, 'pull_request', PR_URL)
    allow_comment(fixture_jira, 'WT-1')
    posted = offer_transition(fixture_jira, 'WT-1', 'In Review')

    handle_command(command(_beta_deployed('gw-25')))

    # No canonical status row changes before the webhook returns.
    assert store.get_work_item(item_id).status == 'proposed'

    comment_texts = posted_comment_texts()
    assert len(comment_texts) == 1
    assert comment_texts[0].startswith('[jenkins] '), 'REQ-09\'s author prefix, added by the comment path'
    assert 'https://hello-world.beta.example.com' in comment_texts[0]
    assert 'deadbeef' in comment_texts[0]

    assert posted == [{'transition': {'id': '10'}}]

    # Evidence posted before the transition (REQ-05's explicit ordering).
    comment_index = next(
        i for i, r in enumerate(FixtureJiraHandler.received)
        if r['method'] == 'POST' and r['path'].endswith('/comment')
    )
    transition_index = next(
        i for i, r in enumerate(FixtureJiraHandler.received)
        if r['method'] == 'POST' and r['path'].endswith('/transitions')
    )
    assert comment_index < transition_index


def test_a_comment_that_fails_transiently_is_retried_before_any_transition(clean_db, fixture_jira):
    item_id = make_item(external_key='WT-RETRY')
    jira_mode()
    store.attach_artifact(item_id, 'pull_request', PR_URL)
    offer_transition(fixture_jira, 'WT-RETRY', 'In Review')

    attempts = {'n': 0}

    def flaky_comment(query, body):
        attempts['n'] += 1
        if attempts['n'] == 1:
            return 500, None
        return 201, {'id': '999'}

    fixture_jira.routes[('POST', '/rest/api/3/issue/WT-RETRY/comment')] = flaky_comment

    envelope = command(_beta_deployed('gw-26'))
    with pytest.raises(Exception):
        handle_command(envelope)

    assert len(requests_to('POST', '/issue/WT-RETRY/comment')) == 1
    assert len(requests_to('POST', '/issue/WT-RETRY/transitions')) == 0, \
        'the transition must not run until the comment has succeeded'

    # Redelivery of the same message (same sourceMessageId, so the same
    # completion key) — a fresh envelope, as the stream's own redelivery
    # would dispatch.
    handle_command(command(_beta_deployed('gw-26')))

    assert len(requests_to('POST', '/issue/WT-RETRY/comment')) == 2
    assert len(requests_to('POST', '/issue/WT-RETRY/transitions')) == 1


def test_a_jira_mode_work_item_with_no_legal_transition_records_its_own_missing_transition_outcome(
        clean_db, fixture_jira):
    """A Jira-mode item whose canonical status is NOT done/cancelled/failed
    (so this handler calls `transition_status`) but whose ticket offers no
    In Review transition on a customized workflow: that outcome is the
    writer's own (REQ-09), recorded once by `jira_writer`, not duplicated
    by this handler."""
    item_id = make_item(external_key='WT-NOTRANS')
    jira_mode()
    store.attach_artifact(item_id, 'pull_request', PR_URL)
    allow_comment(fixture_jira, 'WT-NOTRANS')
    # No "In Review" on offer, and the issue is not already in a status
    # that maps to in-review, so this is a genuine missing-transition
    # outcome.
    offer_transition(fixture_jira, 'WT-NOTRANS', 'Done')
    fixture_jira.routes[('GET', '/rest/api/3/issue/WT-NOTRANS')] = lambda q, b: (
        200, {'fields': {'status': {'name': 'Done'}}}
    )

    handle_command(command(_beta_deployed('gw-27')))

    failures = WebhookFailure.objects.filter(work_item_id=item_id)
    assert failures.count() == 1
    assert 'offers no transition' in failures.get().reason


def test_the_evidence_comment_appears_once_in_core_in_both_modes(clean_db, fixture_jira):
    local_item = make_item(display_name='Local')
    store.attach_artifact(local_item, 'pull_request', PR_URL)
    handle_command(command(_beta_deployed('gw-28')))
    assert WorkItemComment.objects.filter(work_item_id=local_item).count() == 1

    jira_item = make_item(external_key='WT-ONCE', display_name='Jira')
    jira_mode()
    other_pr = f'{PR_URL}-once'
    store.attach_artifact(jira_item, 'pull_request', other_pr)
    allow_comment(fixture_jira, 'WT-ONCE')
    offer_transition(fixture_jira, 'WT-ONCE', 'In Review')

    handle_command(command({
        **_beta_deployed('gw-29', sha='once-sha', build='once-build'),
        'pull_requests': [other_pr],
    }))

    assert len(posted_comment_texts()) == 1
