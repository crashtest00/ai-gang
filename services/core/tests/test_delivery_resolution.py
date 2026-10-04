"""
canonical-delivery-state.md REQ-01, REQ-02 and REQ-03 — the Django side of
delivery resolution.

REQ-02's acceptance asks for the resolver, the normalization both producers
share, the error-level log for an unresolved reference, the new index rather
than a table scan, and `record_completion_marker`'s refusal of the four
association kinds. REQ-01's asks for the deduplication record, the failure
comment, and the retry published with the canonical `workItemId`.

Both of REQ-01's handlers are driven through `command_consumer.handle_command`
— the real dispatch path the gateway's relay reaches — not by calling
`beta_deployment`'s functions directly, because what the requirement claims
is that the gateway's relayed command does these things.
"""

from __future__ import annotations

import json
import logging
import uuid

from workitems import beta_deployment, project_config, registry, store, write_gate
from workitems.command_consumer import handle_command
from workitems.envelope import Kind, build_envelope
from workitems.models import BetaDeploymentRecord, WorkItemArtifact, WorkItemComment

from tests.jira_fixture import permissive_jira  # noqa: F401 - a pytest fixture, used by name

PROJECT = 'test-project'

# The form `gh pr create` prints, an agent passes as `--pull-request`, and
# Jenkins emits as a merged PR's `html_url` (REQ-01).
PR_URL = 'https://github.com/crashtest00/ai-gang/pull/231'


def make_item(display_name='A delivered task'):
    item_id = uuid.uuid4()
    store.create_work_item({'id': item_id, 'project': PROJECT, 'type': 'task', 'displayName': display_name})
    return item_id


def command(payload, project=PROJECT):
    return build_envelope(Kind.WORK_ITEM_COMMAND, project, payload=payload)


# ---------------------------------------------------------------------------
# REQ-01's normalization, shared by REQ-02 and REQ-03
# ---------------------------------------------------------------------------

def test_normalize_reference_lower_cases_the_scheme_host_owner_and_repository():
    assert store.normalize_reference('HTTPS://GitHub.COM/CrashTest00/AI-Gang/pull/231') == \
        'https://github.com/crashtest00/ai-gang/pull/231'


def test_normalize_reference_drops_a_trailing_slash_query_and_fragment():
    assert store.normalize_reference(f'{PR_URL}/') == PR_URL
    assert store.normalize_reference(f'{PR_URL}?w=1') == PR_URL
    assert store.normalize_reference(f'{PR_URL}#files') == PR_URL


def test_normalize_reference_leaves_a_bare_commit_sha_alone():
    """A promoted commit with no merged pull request is carried as its SHA
    (REQ-01), which has no scheme or host to lower-case — and the same
    column carries REQ-19's completion-marker references, which must not be
    rewritten either."""
    assert store.normalize_reference('  0123abcdef  ') == '0123abcdef'


# ---------------------------------------------------------------------------
# REQ-02 — the resolver
# ---------------------------------------------------------------------------

def test_a_pull_request_reference_resolves_to_every_work_item_that_recorded_it(clean_db):
    first = make_item('First')
    second = make_item('Second')
    store.attach_artifact(first, 'pull_request', PR_URL)
    store.attach_artifact(second, 'pull_request', PR_URL)

    resolved = store.resolve_work_items_for_reference(PR_URL, message='a test')

    assert sorted(resolved) == sorted([str(first), str(second)])


def test_a_url_recorded_by_req03_resolves_the_reference_jenkins_emits_for_the_same_pull_request(clean_db):
    """REQ-02's acceptance clause: the agent's `--pull-request` URL and
    Jenkins' `html_url` (or `CHANGE_URL`) for the same pull request resolve
    to each other "including when the two differ only in case or a trailing
    slash"."""
    item_id = make_item()
    store.attach_artifact(item_id, 'pull_request', 'https://GitHub.com/CrashTest00/AI-Gang/pull/231/')

    assert store.resolve_work_items_for_reference(PR_URL, message='a test') == [str(item_id)]


def test_a_reference_with_no_association_is_logged_at_error_level_naming_it_and_the_message(clean_db, caplog):
    with caplog.at_level(logging.ERROR, logger='workitems.store'):
        assert store.resolve_work_items_for_reference(PR_URL, message='beta_deployed msg-77') == []

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert PR_URL in errors[0].getMessage()
    assert 'beta_deployed msg-77' in errors[0].getMessage()


def test_the_resolver_matches_only_the_association_kinds_never_a_completion_marker(clean_db):
    """`artifact_type` is doing double duty: the same column carries
    `canonical-work-model.md` REQ-19's step keys. A marker must never
    answer the resolver."""
    marked = make_item('Has a marker')
    store.record_completion_marker(marked, 'release-check', PR_URL, actor='tester')

    assert WorkItemArtifact.objects.filter(work_item_id=marked, artifact_type='release-check').exists()
    assert store.resolve_work_items_for_reference(PR_URL, message='a test') == []


def test_record_completion_marker_refuses_a_step_key_equal_to_an_association_kind(clean_db):
    """REQ-02's acceptance: "`record_completion_marker` called with a step
    key of `commit`, `pull_request`, `ci_build` or `deployment` raises and
    writes no row" — the other half of the collision rule, so no marker can
    ever be created that the resolver would match."""
    import pytest

    item_id = make_item()
    for step_key in store.ASSOCIATION_KINDS:
        with pytest.raises(store.ValidationError) as excinfo:
            store.record_completion_marker(item_id, step_key, 'anything', actor='tester')
        assert step_key in str(excinfo.value)

    assert WorkItemArtifact.objects.filter(work_item_id=item_id).count() == 0


def test_the_reverse_lookup_uses_the_new_index_rather_than_a_table_scan(clean_db):
    """REQ-02's acceptance: "the lookup uses the new index rather than a
    table scan, demonstrated against a table seeded past the point where a
    scan would be acceptable." Proved by PostgreSQL's own plan for the query
    the resolver issues, which is the only grep-able evidence of a
    schema-level enforcement there can be for an index."""
    from django.db import connection

    item_id = make_item()
    WorkItemArtifact.objects.bulk_create([
        WorkItemArtifact(work_item_id=item_id, artifact_type='commit', reference=f'sha-{n:06d}')
        for n in range(5000)
    ])
    store.attach_artifact(item_id, 'pull_request', PR_URL)

    with connection.cursor() as cursor:
        cursor.execute('ANALYZE work_item_artifact')
        cursor.execute(
            'EXPLAIN SELECT work_item_id FROM work_item_artifact '
            'WHERE artifact_type = ANY(%s) AND reference = %s',
            [list(store.ASSOCIATION_KINDS), PR_URL],
        )
        plan = '\n'.join(row[0] for row in cursor.fetchall())

    assert 'idx_work_item_artifact_ref' in plan, plan
    assert 'Seq Scan' not in plan, plan


# ---------------------------------------------------------------------------
# REQ-03 — the association write is idempotent in `core`
# ---------------------------------------------------------------------------

def test_recording_the_same_pull_request_association_twice_leaves_one_row(clean_db):
    """REQ-03 — "the association write is a no-op when the work item
    already has a `pull_request` row with the same normalized reference".
    ScrumMaster records the URL on every completion and `publishCommand`'s
    `dedupeKey` expires, so `core` is where this has to hold. Driven
    through the `attachArtifact` command, the path ScrumMaster uses."""
    item_id = make_item()

    handle_command(command({'command': 'attachArtifact', 'actor': 'scrummaster',
                             'workItemId': str(item_id), 'artifactType': 'pull_request',
                             'reference': PR_URL}))
    handle_command(command({'command': 'attachArtifact', 'actor': 'scrummaster',
                             'workItemId': str(item_id), 'artifactType': 'pull_request',
                             'reference': f'{PR_URL}/'}))

    rows = WorkItemArtifact.objects.filter(work_item_id=item_id, artifact_type='pull_request')
    assert rows.count() == 1
    assert rows.first().reference == PR_URL, 'stored normalized, so REQ-02 can match it'


def test_a_completion_marker_is_not_deduplicated_by_reference(clean_db):
    """The dedupe is scoped to the association kinds: a step key reusing the
    same column keeps `record_completion_marker`'s own marker check and is
    not touched."""
    item_id = make_item()
    store.attach_artifact(item_id, 'release-check', 'msg-1:release-check')
    store.attach_artifact(item_id, 'release-check', 'msg-1:release-check')

    assert WorkItemArtifact.objects.filter(work_item_id=item_id, artifact_type='release-check').count() == 2


# ---------------------------------------------------------------------------
# REQ-01 — recordPipelineFailure
# ---------------------------------------------------------------------------

def test_record_pipeline_failure_comments_and_publishes_the_retry_with_the_canonical_id(
        clean_db, redis_client):
    item_id = make_item()
    store.attach_artifact(item_id, 'pull_request', PR_URL)

    handle_command(command({
        'command': 'recordPipelineFailure', 'sourceMessageId': 'gw-1',
        'type': 'pipeline_retry', 'pull_requests': [PR_URL],
        'failure_text': 'Pipeline failed on the post-merge dev build.',
        'build_url': 'https://ci.test/job/dev/42/', 'build_number': '42',
    }))

    comment = WorkItemComment.objects.get(work_item_id=item_id)
    assert comment.author == 'jenkins'
    assert 'Pipeline failed on the post-merge dev build.' in comment.body
    assert 'https://ci.test/job/dev/42/' in comment.body
    assert comment.source_message_id == f'gw-1:{item_id}'

    entries = redis_client.xrange(registry.gateway_stream_name(PROJECT))
    assert len(entries) == 1
    envelope = json.loads(entries[0][1]['data'])
    assert envelope['kind'] == 'gateway_operation', 'the shape ScrumMaster already consumes'
    assert envelope['project'] == PROJECT
    assert envelope['payload'] == {'type': 'pipeline_retry', 'workItemId': str(item_id),
                                    'build_url': 'https://ci.test/job/dev/42/', 'build_number': '42'}


def test_a_re_relayed_pipeline_failure_adds_no_second_comment_and_no_second_retry(clean_db, redis_client):
    """REQ-01 — each comment carries `<sourceMessageId>:<workItemId>`, "so a
    redelivery, or a re-relay of the same Jenkins message, adds none". The
    relay carries the GATEWAY envelope's messageId, not the command
    envelope's, which `publishCommand` mints afresh each time — so the two
    commands below differ in `messageId` and agree in `sourceMessageId`."""
    item_id = make_item()
    store.attach_artifact(item_id, 'pull_request', PR_URL)
    payload = {
        'command': 'recordPipelineFailure', 'sourceMessageId': 'gw-2',
        'type': 'pipeline_retry', 'pull_requests': [PR_URL],
        'failure_text': 'Pipeline failed.', 'build_url': 'https://ci.test/job/dev/43/', 'build_number': '43',
    }

    first, second = command(payload), command(payload)
    assert first['messageId'] != second['messageId']
    handle_command(first)
    handle_command(second)

    assert WorkItemComment.objects.filter(work_item_id=item_id).count() == 1
    assert len(redis_client.xrange(registry.gateway_stream_name(PROJECT))) == 1


def test_n_resolved_work_items_get_n_comments_and_n_retries(clean_db, redis_client):
    first = make_item('First')
    second = make_item('Second')
    store.attach_artifact(first, 'pull_request', PR_URL)
    other_pr = 'https://github.com/crashtest00/ai-gang/pull/232'
    store.attach_artifact(second, 'pull_request', other_pr)

    handle_command(command({
        'command': 'recordPipelineFailure', 'sourceMessageId': 'gw-3',
        'type': 'pipeline_retry', 'pull_requests': [PR_URL, other_pr],
        'failure_text': 'Pipeline failed.', 'build_url': 'https://ci.test/job/dev/44/', 'build_number': '44',
    }))

    assert WorkItemComment.objects.filter(work_item_id=first).count() == 1
    assert WorkItemComment.objects.filter(work_item_id=second).count() == 1
    assert len(redis_client.xrange(registry.gateway_stream_name(PROJECT))) == 2


def test_an_unresolved_reference_dispatches_nobody_and_is_logged_at_error_level(clean_db, redis_client, caplog):
    with caplog.at_level(logging.ERROR, logger='workitems.store'):
        result = handle_command(command({
            'command': 'recordPipelineFailure', 'sourceMessageId': 'gw-4',
            'type': 'pipeline_retry', 'pull_requests': [PR_URL],
            'failure_text': 'Pipeline failed.', 'build_url': 'https://ci.test/job/dev/45/',
        }))

    assert result == {'workItemIds': []}
    assert redis_client.xlen(registry.gateway_stream_name(PROJECT)) == 0
    assert WorkItemComment.objects.count() == 0
    assert any(PR_URL in r.getMessage() for r in caplog.records if r.levelno == logging.ERROR)


def test_a_promoted_commit_with_no_merged_pull_request_resolves_as_unresolved(clean_db, redis_client, caplog):
    """REQ-01 — "a commit with no merged pull request, or whose lookup
    failed, is carried as its SHA and resolves as unresolved"."""
    make_item()
    with caplog.at_level(logging.ERROR, logger='workitems.store'):
        handle_command(command({
            'command': 'recordPipelineFailure', 'sourceMessageId': 'gw-5',
            'type': 'pipeline_retry', 'pull_requests': ['0123456789abcdef'],
            'failure_text': 'Pipeline failed.', 'build_url': 'https://ci.test/job/dev/46/',
        }))

    assert redis_client.xlen(registry.gateway_stream_name(PROJECT)) == 0
    assert any('0123456789abcdef' in r.getMessage() for r in caplog.records if r.levelno == logging.ERROR)


# ---------------------------------------------------------------------------
# REQ-01 — recordBetaDeployment's deduplication record
# ---------------------------------------------------------------------------

def _beta_deployed(source_message_id, *, sha='deadbeef', build='hello-world-deadbee-7'):
    return {
        'command': 'recordBetaDeployment', 'sourceMessageId': source_message_id,
        'type': 'beta_deployed', 'pull_requests': [PR_URL],
        'deployed_sha': sha, 'build_identifier': build,
        'build_url': 'https://ci.test/job/dev/47/', 'beta_url': 'https://hello-world.beta.example.com',
    }


def test_record_beta_deployment_writes_one_record_naming_the_work_items_it_resolved(clean_db):
    item_id = make_item()
    store.attach_artifact(item_id, 'pull_request', PR_URL)

    result = handle_command(command(_beta_deployed('gw-10')))

    assert result == {'deduped': False, 'workItemIds': [str(item_id)]}
    record = BetaDeploymentRecord.objects.get()
    assert record.deployed_sha == 'deadbeef'
    assert record.build_identifier == 'hello-world-deadbee-7'
    assert record.source_message_id == 'gw-10'
    assert record.work_item_ids == [str(item_id)]
    assert record.project == PROJECT


def test_a_second_beta_deployed_message_for_the_same_sha_and_build_writes_nothing(clean_db):
    """REQ-01's acceptance: "a second `beta_deployed` message for the same
    deployed SHA and build identifier under a new `messageId` makes no
    second transition, comment or deployment-record row" — the gateway
    deduplicates by `messageId` alone, so this is `core`'s job."""
    item_id = make_item()
    store.attach_artifact(item_id, 'pull_request', PR_URL)

    handle_command(command(_beta_deployed('gw-11')))
    result = handle_command(command(_beta_deployed('gw-12')))  # a NEW source message id.

    assert result['deduped'] is True
    assert BetaDeploymentRecord.objects.count() == 1


def test_the_deduplication_is_enforced_by_a_database_constraint_not_a_read_then_write(clean_db):
    """Gate 3: the claim "unique on deployed SHA and build identifier" has
    to point at a real constraint. `uq_beta_deployment_sha_build` is it."""
    import pytest
    from django.db import IntegrityError, transaction

    BetaDeploymentRecord.objects.create(project=PROJECT, deployed_sha='s', build_identifier='b')
    with pytest.raises(IntegrityError), transaction.atomic():
        BetaDeploymentRecord.objects.create(project=PROJECT, deployed_sha='s', build_identifier='b')


def test_a_different_build_of_the_same_sha_is_a_different_deployment(clean_db):
    item_id = make_item()
    store.attach_artifact(item_id, 'pull_request', PR_URL)

    handle_command(command(_beta_deployed('gw-13', build='hello-world-deadbee-7')))
    result = handle_command(command(_beta_deployed('gw-14', build='hello-world-deadbee-8')))

    assert result['deduped'] is False
    assert BetaDeploymentRecord.objects.count() == 2


def test_the_relay_carries_the_gateway_messageid_not_the_command_envelopes_own(clean_db):
    """REQ-01 — "The relay carries the gateway envelope's `messageId` as the
    command's `sourceMessageId`, and REQ-01's and REQ-05's keys use that
    `sourceMessageId`, not the command envelope's own `messageId`, which
    `publishCommand` mints afresh on each relay." With no `sourceMessageId`
    in the payload the envelope's own id is the fallback, so a producer that
    omits it still gets a stable key within one relay."""
    item_id = make_item()
    store.attach_artifact(item_id, 'pull_request', PR_URL)
    payload = _beta_deployed('gw-15')

    envelope = command(payload)
    handle_command(envelope)

    record = BetaDeploymentRecord.objects.get()
    assert record.source_message_id == 'gw-15'
    assert record.source_message_id != envelope['messageId']
