"""
canonical-delivery-state.md REQ-10 — `disconnect_jira`, the only path back
to local mode, through `call_command`.

The Jira side is the STRICT fixture with no route registered at all: any
Jira call this command made would 404 and raise, so "makes no Jira write"
is enforced rather than asserted.
"""

from __future__ import annotations

import io
import uuid

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from workitems import project_config, store

from tests.jira_fixture import fixture_jira  # noqa: F401 - a pytest fixture, used by name

PROJECT = 'test-project'


def make_item(*, item_type='task', status='proposed', display_name='A task', external_key=None):
    item_id = uuid.uuid4()
    store.create_work_item({
        'id': item_id, 'project': PROJECT, 'type': item_type, 'displayName': display_name,
        'status': status, 'externalKey': external_key,
    })
    return item_id


def run(project=PROJECT):
    out = io.StringIO()
    call_command('disconnect_jira', project, stdout=out)
    return out.getvalue()


def test_returns_a_jira_mode_project_to_local_with_no_jira_write(clean_db, fixture_jira):  # noqa: F811
    """REQ-10's acceptance: "`disconnect_jira` returns a Jira-mode project
    to local mode through `project_config.revert_to_local` with no Jira
    write"."""
    # Created while the project is still local: a Jira-mode create is
    # refused (REQ-09), which is the state this command exists to leave.
    make_item(external_key='TP-1')
    project_config.set_mode(PROJECT, 'jira', jira_project_key='TP')

    output = run()

    assert project_config.get_mode(PROJECT) == {
        'project': PROJECT, 'mode': 'local', 'jiraProjectKey': 'TP',
    }, 'the recorded key is kept, which is what lets connect_jira re-sync the same Jira project'
    assert fixture_jira.received == [], 'not one Jira call'
    assert 'local mode' in output


def test_the_work_items_keep_their_keys(clean_db, fixture_jira):  # noqa: F811
    """Nothing is reconciled on the way back: the keyed items keep their
    keys, which is what makes `connect_jira`'s re-sync resumable and what
    §4's repair path (disconnect, edit in the admin, connect) depends on."""
    item = make_item(external_key='TP-1')
    project_config.set_mode(PROJECT, 'jira', jira_project_key='TP')

    run()

    assert store.get_work_item(item).external_key == 'TP-1'


def test_refuses_while_a_release_of_the_project_is_open(clean_db, fixture_jira):  # noqa: F811
    """REQ-10's acceptance: "`disconnect_jira` refuses while a Release of
    the project is open" (Pass 6, decision 4.1) — while the project is
    local `core` ignores the Release's Jira webhooks, so a Release left
    open across the disconnect could only be closed by a person in the
    admin, which publishes a release event (REQ-08)."""
    make_item(item_type='release', display_name='R1', status='in-review', external_key='TP-9')
    project_config.set_mode(PROJECT, 'jira', jira_project_key='TP')

    with pytest.raises(CommandError) as err:
        run()

    assert 'R1' in str(err.value)
    assert project_config.get_mode(PROJECT)['mode'] == 'jira', 'it changed nothing'
    assert fixture_jira.received == []


def test_a_closed_release_does_not_refuse(clean_db, fixture_jira):  # noqa: F811
    make_item(item_type='release', display_name='R1', status='done', external_key='TP-9')
    project_config.set_mode(PROJECT, 'jira', jira_project_key='TP')

    run()

    assert project_config.get_mode(PROJECT)['mode'] == 'local'


def test_disconnecting_a_project_that_was_never_connected_is_a_no_op(clean_db, fixture_jira):  # noqa: F811
    run()

    assert project_config.get_mode(PROJECT)['mode'] == 'local'
    assert fixture_jira.received == []
