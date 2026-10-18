"""
canonical-delivery-state.md REQ-10 — `ProjectConfigAdmin` shows `mode` and
`jira_project_key` read-only on add as well as change, and deletes no row,
"so no admin edit switches a project's mode" (Shovel Ready Pass 5, decision
6.2).

Driven through the admin's real HTTP surface — the add and change pages and
the change list's `delete_selected` action — rather than through the
ModelAdmin's methods, because the REQ is about what an operator can do in
the browser.
"""

from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse

from workitems import project_config
from workitems.models import ProjectConfig

PROJECT = 'test-project'


@pytest.fixture
def admin_client(clean_db):
    User.objects.create_superuser('admin', 'admin@example.com', 'password')
    client = Client()
    client.force_login(User.objects.get(username='admin'))
    return client


def test_the_change_page_renders_mode_and_key_read_only(admin_client):
    project_config.set_mode(PROJECT, 'jira', jira_project_key='TP')

    response = admin_client.get(reverse('admin:workitems_projectconfig_change', args=[PROJECT]))

    body = response.content.decode()
    assert response.status_code == 200
    assert 'name="mode"' not in body, 'a read-only field renders as text, not an input'
    assert 'name="jira_project_key"' not in body


def test_the_add_page_renders_them_read_only_too(admin_client):
    """"on add as well as change" — otherwise a new row could be created
    straight into Jira mode, with no push and no `connect_jira`."""
    response = admin_client.get(reverse('admin:workitems_projectconfig_add'))

    body = response.content.decode()
    assert response.status_code == 200
    assert 'name="project"' in body, 'the project name is still the operator\'s to type'
    assert 'name="mode"' not in body
    assert 'name="jira_project_key"' not in body


def test_a_posted_mode_change_is_ignored(admin_client):
    """Django ignores a read-only field's posted value rather than
    rejecting it, which is the behaviour that matters: an operator who
    crafts the POST by hand still cannot switch the mode."""
    project_config.set_mode(PROJECT, 'local')

    admin_client.post(reverse('admin:workitems_projectconfig_change', args=[PROJECT]),
                       {'project': PROJECT, 'mode': 'jira', 'jira_project_key': 'TP'})

    assert project_config.get_mode(PROJECT) == {
        'project': PROJECT, 'mode': 'local', 'jiraProjectKey': None,
    }


def test_the_change_list_offers_no_delete_action(admin_client):
    """REQ-10: it "MUST NOT delete a row, because a project with no row is
    local" — deleting a Jira-mode project's row would switch it back with
    no `disconnect_jira` and no record of it."""
    project_config.set_mode(PROJECT, 'jira', jira_project_key='TP')

    response = admin_client.get(reverse('admin:workitems_projectconfig_changelist'))

    assert 'delete_selected' not in response.content.decode()


def test_a_bulk_delete_selecting_a_jira_mode_row_deletes_nothing(admin_client):
    """The action is not offered; posted by hand it deletes nothing. A
    selection containing any Jira-mode row is refused whole, its local rows
    included, as every other bulk delete in this module is (REQ-09)."""
    project_config.set_mode(PROJECT, 'jira', jira_project_key='TP')
    project_config.set_mode('other-project', 'local')

    admin_client.post(reverse('admin:workitems_projectconfig_changelist'), {
        'action': 'delete_selected', '_selected_action': [PROJECT, 'other-project'],
        'post': 'yes',
    })

    assert set(ProjectConfig.objects.values_list('project', flat=True)) == {PROJECT, 'other-project'}


def test_the_delete_page_deletes_nothing(admin_client):
    project_config.set_mode(PROJECT, 'jira', jira_project_key='TP')

    admin_client.post(reverse('admin:workitems_projectconfig_delete', args=[PROJECT]),
                       {'post': 'yes'})

    assert ProjectConfig.objects.filter(project=PROJECT).exists()
    assert project_config.get_mode(PROJECT)['mode'] == 'jira'
