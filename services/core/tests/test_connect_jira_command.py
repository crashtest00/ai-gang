"""
canonical-delivery-state.md REQ-10 — `connect_jira`, against a fixture Jira
API.

Every test drives `call_command('connect_jira', ...)`, Django's real entry
point for the command an operator runs with
`docker exec core-api python manage.py connect_jira <project> <key>`, never
its helper functions: the REQ is about what happens when the command runs,
including which refusal comes first and what is left behind when a step
fails.

The Jira side is `tests/jira_fixture.py`'s `FakeJiraInstance` — a real HTTP
round trip per call, with issues that remember their status, fields and
links, so the re-sync's reads see what its own writes did.
"""

from __future__ import annotations

import io
import os
import uuid

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from workitems import jira_writer, project_config, store
from workitems.management.commands.connect_jira import JIRA_FIELD_ID_VARS
from workitems.management.commands.ensure_jira_webhook import WEBHOOK_EVENTS
from workitems.models import JiraWriteCompletion, ProjectConfig, WebhookFailure, WorkItem

from tests.jira_fixture import jira_instance, permissive_jira  # noqa: F401 - pytest fixtures, used by name
from tests.test_field_id_scripts import derive_env_field_id_vars

PROJECT = 'test-project'

# The two the fixture sets itself; the rest are set here, because REQ-10
# refuses to connect unless all fourteen are present.
_FIXTURE_SET = ('JIRA_AGENT_FIELD_ID', 'JIRA_BLOCKED_FIELD_ID')


@pytest.fixture
def jira_env(jira_instance, monkeypatch):  # noqa: F811 - the fixture, by name
    """A fully configured environment: the fixture's credentials and Agent/
    Blocked ids, plus the other twelve field ids REQ-10 requires, and a
    registered webhook carrying every `WEBHOOK_EVENTS` entry."""
    for index, name in enumerate(JIRA_FIELD_ID_VARS):
        if name not in _FIXTURE_SET:
            monkeypatch.setenv(name, f'customfield_2{index:04d}')
    register_webhook(jira_instance)
    return jira_instance


def register_webhook(instance, *, events=None, path='/webhooks/jira'):
    instance.handler.routes[('GET', '/rest/webhooks/1.0/webhook')] = lambda q, b: (200, [{
        'name': 'AI Gang',
        'url': f'https://hq.example.com{path}?secret=s3cret',
        'events': list(events if events is not None else WEBHOOK_EVENTS),
        'self': 'https://example.atlassian.net/rest/webhooks/1.0/webhook/1',
    }])


def run(project=PROJECT, key='TP'):
    out = io.StringIO()
    call_command('connect_jira', project, key, stdout=out)
    return out.getvalue()


def make_item(*, status='proposed', item_type='task', display_name='A task', parent_id=None,
               assignee=None, external_key=None, project=PROJECT, story_detail=None):
    item_id = uuid.uuid4()
    store.create_work_item({
        'id': item_id, 'project': project, 'type': item_type, 'displayName': display_name,
        'status': status, 'parentId': parent_id, 'assigneeAgentId': assignee,
        'externalKey': external_key, 'storyDetail': story_detail,
    })
    return item_id


def mode(project=PROJECT):
    return project_config.get_mode(project)


# ---------------------------------------------------------------------------
# The refusals, each changing nothing
# ---------------------------------------------------------------------------

def test_refuses_while_a_required_variable_holds_a_placeholder(clean_db, jira_env, monkeypatch):
    """REQ-10: it refuses "unless `JIRA_URL`, `JIRA_EMAIL`, `JIRA_TOKEN` and
    every id in `derive-env.sh`'s `JIRA_FIELD_ID_VARS` are set, non-blank
    and not one of `jira_client.ENV_PLACEHOLDERS`"."""
    make_item()
    monkeypatch.setenv('JIRA_CANDIDATE_SHA_FIELD_ID', 'customfield_XXXXX')

    with pytest.raises(CommandError) as err:
        run()

    assert 'JIRA_CANDIDATE_SHA_FIELD_ID' in str(err.value)
    assert mode()['mode'] == 'local'
    assert mode()['jiraProjectKey'] is None, 'it changed nothing — not even the key'
    assert jira_env.created == []


def test_refuses_while_a_required_variable_is_unset(clean_db, jira_env, monkeypatch):
    make_item()
    monkeypatch.delenv('JIRA_PREVIEW_URL_FIELD_ID')

    with pytest.raises(CommandError) as err:
        run()

    assert 'JIRA_PREVIEW_URL_FIELD_ID' in str(err.value)
    assert mode()['mode'] == 'local'


def test_the_required_field_ids_are_derive_envs_own_list(clean_db):
    """Gate 4 — an enumerating list matches the config that defines it. The
    command refuses on this list, and `derive-env.sh`'s loop is what puts
    the ids into this service's environment, so a variable in one and not
    the other is either a refusal an operator cannot satisfy or a field id
    that silently never arrives."""
    assert list(JIRA_FIELD_ID_VARS) == derive_env_field_id_vars()


def test_refuses_while_no_registered_webhook_carries_every_event(clean_db, jira_env, jira_instance):  # noqa: F811
    """REQ-10: "unless Jira lists a webhook whose URL path is
    `/webhooks/jira` and whose events include every `WEBHOOK_EVENTS`
    entry". Connecting a project whose inbound half is missing an event
    leaves `core` blind to exactly the changes Jira mode depends on."""
    make_item()
    register_webhook(jira_instance, events=['jira:issue_created', 'jira:issue_updated'])

    with pytest.raises(CommandError) as err:
        run()

    assert 'comment_created' in str(err.value)
    assert 'issuelink_created' in str(err.value)
    assert mode()['mode'] == 'local'
    assert jira_env.created == []


def test_refuses_while_a_webhook_is_registered_at_another_path(clean_db, jira_env, jira_instance):  # noqa: F811
    make_item()
    register_webhook(jira_instance, path='/some/other/hook')

    with pytest.raises(CommandError):
        run()

    assert mode()['mode'] == 'local'


def test_refuses_while_a_release_work_item_is_open(clean_db, jira_env):
    """REQ-10: it refuses "while any `release` work item of the project is
    open (its baseline not `done`, `cancelled` or `failed`)"."""
    make_item()
    make_item(item_type='release', display_name='R1', status='in-review')

    with pytest.raises(CommandError) as err:
        run()

    assert 'R1' in str(err.value)
    assert mode()['mode'] == 'local'
    assert jira_env.created == []


def test_a_closed_release_does_not_refuse_and_is_not_pushed(clean_db, jira_env):
    """The other half of the same rule, and Pass 7 answer 2.1: "A `release`
    is neither pushed nor re-synced"."""
    task = make_item()
    make_item(item_type='release', display_name='R1', status='done')

    run()

    assert mode()['mode'] == 'jira'
    assert len(jira_env.created) == 1, 'the task only — a release is never pushed'
    assert store.get_work_item(task).external_key == 'TP-1'
    assert [item.external_key for item in WorkItem.objects.filter(type='release')] == [None]


def test_a_keyed_release_gets_no_resync_write(clean_db, jira_env, jira_instance):  # noqa: F811
    """"a keyed Release gets no re-sync write" — its issue is left exactly
    as Jira has it, because a re-sync move to Done would publish `done`
    and promote its candidate (REQ-08)."""
    jira_instance.add_issue('TP-9', issuetype='Release', status='Backlog')
    make_item(item_type='release', display_name='R1', status='done', external_key='TP-9')
    make_item()

    run()

    assert jira_instance.status_of('TP-9') == 'Backlog'
    assert jira_instance.blocked('TP-9') is None


def test_refuses_a_project_already_in_jira_mode_and_moves_no_consumer_group(
        clean_db, jira_env, redis_client):
    """REQ-10's acceptance: "`connect_jira` refuses a project already in
    Jira mode and moves no consumer group". The refusal comes from
    `project_config.set_jira_project_key`, so the command reads no mode
    itself (REQ-09, "Where the mode is read")."""
    from workitems.stream_topology import event_stream_name

    project_config.set_mode(PROJECT, 'jira', jira_project_key='TP')
    stream = event_stream_name(PROJECT)
    redis_client.xadd(stream, {'envelope': '{}'})
    redis_client.xgroup_create(stream, jira_writer.WRITER_GROUP, id='0')

    with pytest.raises(CommandError) as err:
        run()

    assert 'already in Jira mode' in str(err.value)
    groups = {group['name']: group for group in redis_client.xinfo_groups(stream)}
    assert groups[jira_writer.WRITER_GROUP]['last-delivered-id'] == '0-0', \
        'the group is left where it was'


# ---------------------------------------------------------------------------
# The push
# ---------------------------------------------------------------------------

def test_pushes_one_issue_per_item_with_subtasks_under_their_parent(clean_db, jira_env):
    """REQ-10's acceptance: "creates exactly one Jira issue per item other
    than a release, subtasks under their parent's issue"."""
    story = make_item(item_type='story', display_name='A story', status='proposed',
                       assignee='refinement-agent')
    child = make_item(display_name='A child', parent_id=story, assignee='backend-agent')

    run()

    story_row = store.get_work_item(story)
    child_row = store.get_work_item(child)
    assert story_row.external_key and child_row.external_key
    assert len(jira_env.created) == 2
    created = {entry['key']: entry['fields'] for entry in jira_env.created}
    assert created[story_row.external_key]['issuetype'] == {'name': 'Story'}
    assert created[child_row.external_key]['issuetype'] == {'name': 'Sub-task'}
    assert created[child_row.external_key]['parent'] == {'key': story_row.external_key}
    assert jira_env.agent(story_row.external_key) == 'refinement-agent'
    assert jira_env.agent(child_row.external_key) == 'backend-agent'


def test_a_pushed_story_carries_its_story_fields_in_adf(clean_db, jira_env):
    """REQ-10's acceptance: "each created with its Agent value and, for a
    story, its story fields as the fixture records them in Atlassian
    Document Format"."""
    from workitems import jira_interpret

    story = make_item(item_type='story', display_name='A story', assignee='refinement-agent',
                       story_detail={
                           'behavior': 'It does the thing.\nOn two lines.',
                           'acceptanceCriteria': 'Given X, then Y',
                           'constraints': 'None',
                           'edgeCases': 'None',
                           'outOfScope': 'Everything else',
                       })

    run()

    key = store.get_work_item(story).external_key
    # Read off the issue the fixture RECORDED, not off the request we sent,
    # so this is what Jira ended up holding.
    recorded = jira_env.issues[key]['fields']
    behavior = recorded[os.environ['JIRA_BEHAVIOR_FIELD_ID']]
    assert behavior['type'] == 'doc', 'Atlassian Document Format, not a bare string'
    # One paragraph per line, which is what `adf_to_text` reads back, so a
    # multi-line field survives the round trip REQ-10 names.
    assert jira_interpret.adf_to_text(behavior).strip() == 'It does the thing.\nOn two lines.'
    assert jira_interpret.adf_to_text(recorded[os.environ['JIRA_AC_FIELD_ID']]).strip() == \
        'Given X, then Y'
    assert jira_env.agent(key) == 'refinement-agent', 'the Agent field, in the same request'


def test_the_key_is_recorded_while_the_project_is_still_local(clean_db, jira_env, monkeypatch):
    """REQ-10's acceptance: "the Jira key is recorded with
    `set_jira_project_key` while the project is still local". Observed by
    failing the push: the key survives, the mode does not change."""
    make_item()
    monkeypatch.setattr(
        'workitems.jira_client.create_issue',
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('Jira said no')),
    )

    with pytest.raises(CommandError):
        run()

    assert mode() == {'project': PROJECT, 'mode': 'local', 'jiraProjectKey': 'TP'}


def test_a_failed_create_leaves_the_project_local_and_a_rerun_resumes(clean_db, jira_env, monkeypatch):
    """REQ-10's acceptance: "a create that fails permanently leaves the
    project local and exits non-zero naming it, and a re-run after the
    cause is fixed pushes it and switches"; "a create or re-sync step that
    fails is recorded as a webhook failure naming the item and the step";
    and "Re-running it skips every item that already has a key, so it
    resumes"."""
    first = make_item(display_name='First')
    second = make_item(display_name='Second')
    from workitems import jira_client

    real_create = jira_client.create_issue
    state = {'calls': 0, 'failing': True}

    def flaky(*args, **kwargs):
        state['calls'] += 1
        if state['failing'] and state['calls'] == 2:
            raise RuntimeError('Jira said no')
        return real_create(*args, **kwargs)

    monkeypatch.setattr('workitems.jira_client.create_issue', flaky)

    with pytest.raises(CommandError) as err:
        run()

    assert 'Second' in str(err.value) or 'First' in str(err.value)
    assert mode()['mode'] == 'local'
    keyed = [item for item in WorkItem.objects.filter(project=PROJECT) if item.external_key]
    assert len(keyed) == 1, 'the one that succeeded keeps its key'
    failures = list(WebhookFailure.objects.all())
    assert len(failures) == 1
    assert failures[0].payload['step'] == 'connect-jira-create'
    assert 'connect_jira' in failures[0].reason

    # The cause is fixed, and nothing else about the environment changes —
    # `monkeypatch.undo()` would also take back this test's field ids.
    state['failing'] = False
    run()

    assert mode()['mode'] == 'jira'
    assert all(item.external_key for item in WorkItem.objects.filter(project=PROJECT))
    assert len(jira_env.created) == 2, 'the item that already had a key was not created again'
    _ = first, second


# ---------------------------------------------------------------------------
# The re-sync (REQ-10's own outcomes over the writer's status rules)
# ---------------------------------------------------------------------------

def test_the_resync_sets_each_items_mapped_jira_status(clean_db, jira_env):
    """REQ-10's acceptance: "sets each item's mapped Jira status". Applied
    "although the project is still local" — `connect_jira` is REQ-09's one
    exception to the writer's rule that it writes no Jira for a project not
    then in Jira mode."""
    in_progress = make_item(status='in-progress', display_name='Working')
    in_review = make_item(status='in-review', display_name='Reviewing')
    done = make_item(status='done', display_name='Finished')

    run()

    statuses = {
        store.get_work_item(item).display_name: jira_env.status_of(store.get_work_item(item).external_key)
        for item in (in_progress, in_review, done)
    }
    assert statuses == {'Working': 'In Progress', 'Reviewing': 'In Review', 'Finished': 'Done'}
    assert mode()['mode'] == 'jira'


def test_a_status_the_map_does_not_cover_is_left_as_jira_has_it(clean_db, jira_env):
    """REQ-10's acceptance: "an item in `assigned` or
    `waiting-on-dependency`, which the default map does not cover, gets no
    transition, records no failure and does not stop the switch". Pass 7
    answer 1, and the OPPOSITE of the inbound webhook rule, which records a
    failure for an unmapped status."""
    assigned = make_item(status='assigned', display_name='Assigned', assignee='backend-agent')
    waiting = make_item(status='waiting-on-dependency', display_name='Waiting')

    run()

    for item in (assigned, waiting):
        key = store.get_work_item(item).external_key
        assert jira_env.status_of(key) == 'Backlog', 'left exactly as Jira created it'
        assert jira_env.blocked(key) is None
    assert WebhookFailure.objects.count() == 0, 'neither a write, a rejection nor a failed step'
    assert mode()['mode'] == 'jira', 'and it does not stop the switch'


def test_needs_clarification_failed_and_cancelled_set_the_blocked_flag(clean_db, jira_env):
    """REQ-10's acceptance: "an item in `needs-clarification`, `failed` or
    `cancelled` gets the Blocked flag set and no transition" — Jira shows
    all three as one flag (§4)."""
    items = [make_item(status=status, display_name=status)
             for status in ('needs-clarification', 'failed', 'cancelled')]

    run()

    for item in items:
        key = store.get_work_item(item).external_key
        assert jira_env.blocked(key) == {'value': 'Yes'}
        assert jira_env.status_of(key) == 'Backlog', 'the flag, not a transition'
    assert WebhookFailure.objects.count() == 0
    assert mode()['mode'] == 'jira'


def test_an_issue_already_in_a_mapping_status_counts_done_even_while_flagged(
        clean_db, jira_env, jira_instance):  # noqa: F811
    """REQ-10's acceptance: "an issue already in its status, or in another
    Jira status mapping to the same canonical status, counts as done while
    flagged" — unlike a writer push, which counts it done only with the
    flag clear, "because the re-sync clears no flag"."""
    project_config.declare_custom_status(PROJECT, 'in-review', 'in-review', 'In Review')
    project_config.declare_custom_status(PROJECT, 'tester-acceptance', 'in-review', 'In Review')
    # A workflow that offers NO transition to In Review from where it is,
    # so the only way this can pass is the already-mapped rule.
    jira_instance.add_issue('TP-7', status='In Review', offered=['Done'],
                             fields={jira_instance.blocked_field: {'value': 'Yes'}})
    make_item(status='in-review', display_name='Reviewing', external_key='TP-7')

    run()

    assert WebhookFailure.objects.count() == 0, 'counted done, flagged or not'
    assert jira_instance.blocked('TP-7') == {'value': 'Yes'}, 'and no flag is cleared'
    assert mode()['mode'] == 'jira'


def test_a_missing_transition_with_no_mapping_status_is_a_failed_step(
        clean_db, jira_env, jira_instance):  # noqa: F811
    """REQ-10: "a transition the issue does not offer, with the issue in no
    Jira status that maps to the item's canonical status ... counts as
    failed", and "a re-sync transition ... that fails permanently leaves the
    project local and exits non-zero naming it"."""
    jira_instance.add_issue('TP-7', status='In Progress', offered=['Done'])
    make_item(status='in-review', display_name='Reviewing', external_key='TP-7')

    with pytest.raises(CommandError) as err:
        run()

    assert 'Reviewing' in str(err.value)
    assert mode()['mode'] == 'local'
    failure = WebhookFailure.objects.get()
    assert failure.payload['step'] == 'connect-jira-status'
    assert failure.payload['targetJiraStatus'] == 'In Review'


def test_the_resync_creates_a_blocks_link_per_canonical_link_once(clean_db, jira_env, jira_instance):  # noqa: F811
    """REQ-10's acceptance: "and its Blocks links, and then switches the
    project to Jira mode; a second run creates no issue and no second
    Blocks link, the existing link being found with `get_issue_links`"."""
    blocker = make_item(status='done', display_name='Blocker')
    dependent = make_item(status='waiting-on-dependency', display_name='Dependent')
    store.create_link(blocker, dependent, 'blocks')

    run()

    blocker_key = store.get_work_item(blocker).external_key
    dependent_key = store.get_work_item(dependent).external_key
    assert jira_instance.links == [(blocker_key, dependent_key)]

    # A second run: the project is in Jira mode now, so it is refused —
    # which is itself REQ-10's rule. Disconnect and re-run, as REQ-10 says
    # a re-sync is done, and the link must not be created twice.
    call_command('disconnect_jira', PROJECT, stdout=io.StringIO())
    created_before = len(jira_env.created)
    run()

    assert jira_instance.links == [(blocker_key, dependent_key)], 'found with get_issue_links'
    assert len(jira_env.created) == created_before, 'and no issue created again'


def test_a_link_whose_other_end_is_a_release_is_not_pushed(clean_db, jira_env, jira_instance):  # noqa: F811
    """"for each canonical `blocks` link whose two ends both have keys" — a
    release has none, because it is never pushed."""
    release = make_item(item_type='release', status='done', display_name='R1')
    dependent = make_item(status='waiting-on-dependency', display_name='Dependent')
    store.create_link(release, dependent, 'blocks')

    run()

    assert jira_instance.links == []
    assert mode()['mode'] == 'jira'


# ---------------------------------------------------------------------------
# The switch
# ---------------------------------------------------------------------------

def test_the_switch_moves_the_writers_group_to_the_streams_end(clean_db, jira_env, redis_client):
    """REQ-10: "it switches the project to Jira mode in one transaction and
    moves the writer's consumer group on the project's events stream to the
    stream's end, so nothing published while the project was local is
    written to Jira"; acceptance: "an event published before the switch
    makes no Jira write after it"."""
    from workitems import jira_writer as writer
    from workitems.stream_topology import event_stream_name

    item = make_item(status='in-progress', display_name='Working')
    stream = event_stream_name(PROJECT)
    # Everything the project published while it was local — including a
    # release-candidate event, which the writer WOULD write to Jira.
    before = redis_client.xadd(stream, {'envelope': '{}'})
    redis_client.xgroup_create(stream, writer.WRITER_GROUP, id='0')

    run()

    groups = {group['name']: group for group in redis_client.xinfo_groups(stream)}
    group = groups[writer.WRITER_GROUP]
    assert group['last-delivered-id'] != '0-0'
    assert group['last-delivered-id'] >= before, 'moved past everything published while local'
    assert redis_client.xreadgroup(writer.WRITER_GROUP, 'test', {stream: '>'}, count=10) in ([], None)
    _ = item


def test_the_switch_is_withheld_while_any_step_failed(clean_db, jira_env, jira_instance):  # noqa: F811
    """REQ-10, Pass 6 SR-6-26: "each failed create or re-sync step recorded
    and the switch withheld while any failed". One good item is pushed and
    keeps its key; the project stays local."""
    good = make_item(status='in-progress', display_name='Good')
    jira_instance.add_issue('TP-7', status='In Progress', offered=['Done'])
    make_item(status='in-review', display_name='Bad', external_key='TP-7')

    with pytest.raises(CommandError):
        run()

    assert mode()['mode'] == 'local'
    assert store.get_work_item(good).external_key is not None
    assert jira_env.status_of(store.get_work_item(good).external_key) == 'In Progress', \
        'the good item was still re-synced'


def test_a_pushed_storys_issue_created_after_the_switch_adds_no_row_and_no_intake_comment(
        clean_db, jira_env, jira_instance):  # noqa: F811
    """REQ-10's acceptance: "a pushed Story's `jira:issue_created` processed
    after the switch adds no row and posts no story-intake comment" — its
    key already has a row, so `_handle_story_created`'s early return runs
    and registers no comment step (REQ-09, "Canonical events with a Jira
    side effect")."""
    from workitems.webhook_consumer import handle_webhook_envelope
    from workitems.models import WorkItemComment

    story = make_item(item_type='story', display_name='A story', assignee='refinement-agent',
                       story_detail={'behavior': 'b', 'acceptanceCriteria': 'a', 'constraints': 'c',
                                      'edgeCases': 'e', 'outOfScope': 'o'})
    run()
    key = store.get_work_item(story).external_key
    rows_before = WorkItem.objects.count()
    comments_before = WorkItemComment.objects.count()
    posted_before = len([r for r in jira_instance.handler.received if r['path'].endswith('/comment')])

    handle_webhook_envelope({
        'messageId': 'webhook-1', 'project': PROJECT,
        'payload': {'event': 'jira:issue_created', 'issue': jira_instance._issue_body(key, {}),
                     'body': {}},
    })

    assert WorkItem.objects.count() == rows_before
    assert WorkItemComment.objects.count() == comments_before
    assert len([r for r in jira_instance.handler.received if r['path'].endswith('/comment')]) == \
        posted_before, 'and no story-intake comment is posted to Jira either'
    assert JiraWriteCompletion.objects.filter(completion_key__startswith='webhook-1:').count() == 0


def test_an_item_that_already_had_a_key_is_not_created_again_but_is_resynced(
        clean_db, jira_env, jira_instance):  # noqa: F811
    """REQ-10's acceptance: "an item other than a release that already had a
    key is not created again and its Jira status is set to its mapped one"
    — the re-sync on reconnect `../../v5.1/RELEASE.md` §6 left to this
    stage."""
    jira_instance.add_issue('TP-5', status='Backlog')
    keyed = make_item(status='in-progress', display_name='Already keyed', external_key='TP-5')
    fresh = make_item(status='done', display_name='Fresh')

    run()

    assert [entry['key'] for entry in jira_env.created] == [store.get_work_item(fresh).external_key]
    assert store.get_work_item(keyed).external_key == 'TP-5', 'its key is untouched'
    assert jira_instance.status_of('TP-5') == 'In Progress', 'and its status is re-synced'
    assert mode()['mode'] == 'jira'
