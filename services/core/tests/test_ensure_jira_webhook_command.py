"""
BUGFIXES.md BF-02 point 1: Jira webhook registration moved out of
scripts/init-project.sh's deleted `ensure_webhook()` and into this
service's own `manage.py ensure_jira_webhook` (workitems/management/
commands/ensure_jira_webhook.py).

Every test here goes through `call_command` — Django's real entry point
for a management command, the same one `docker exec ... python manage.py
ensure_jira_webhook` (scripts/init-project.sh) invokes — never the bare
`ensure_jira_webhook()` helper function directly, per the enforcement-point
rule: the REQ this satisfies is "registration happens when the command
runs," not "the helper function computes the right thing in isolation."

The Jira HTTP call is mocked at `_jira_request`, the one function in the
command module that touches the network — no test here makes a real
request to anything.
"""

from __future__ import annotations

import io
import pathlib

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from workitems.management.commands import ensure_jira_webhook as command_module

WEBHOOK_URL = 'https://hq.example.com/webhooks/jira?secret=s3cr3t'


def _set_required_env(monkeypatch, **overrides):
    values = {
        'JIRA_URL': 'https://example.atlassian.net',
        'JIRA_EMAIL': 'bot@example.com',
        'JIRA_TOKEN': 'token-123',
        'HQ_URL': 'https://hq.example.com',
        'WEBHOOK_SECRET': 's3cr3t',
    }
    values.update(overrides)
    for name, value in values.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


def _run_command():
    out = io.StringIO()
    call_command('ensure_jira_webhook', stdout=out)
    return out.getvalue()


def test_registers_the_live_plural_webhooks_route_when_none_exists(monkeypatch):
    _set_required_env(monkeypatch)
    calls = []

    def fake_request(method, url, email, token, body=None):
        calls.append((method, url, email, token, body))
        if method == 'GET':
            return []
        return {'self': 'https://example.atlassian.net/rest/webhooks/1.0/webhook/1'}

    monkeypatch.setattr(command_module, '_jira_request', fake_request)

    output = _run_command()

    assert 'Webhook registered' in output
    assert WEBHOOK_URL in output
    # Exactly one GET (existence check) and one POST (registration).
    methods = [c[0] for c in calls]
    assert methods == ['GET', 'POST']
    # The registered URL is the live plural route, not the deleted singular one.
    post_body = calls[1][4]
    assert post_body['url'] == WEBHOOK_URL
    assert '/webhooks/jira' in post_body['url']
    assert '/webhook/jira' not in post_body['url']
    # Basic-auth credentials passed through to Jira, not something derived.
    assert calls[0][2] == 'bot@example.com'
    assert calls[0][3] == 'token-123'


def test_is_idempotent_and_skips_the_write_when_already_registered_with_every_event(monkeypatch):
    _set_required_env(monkeypatch)
    calls = []

    def fake_request(method, url, email, token, body=None):
        calls.append(method)
        if method == 'GET':
            return [{'url': WEBHOOK_URL, 'self': 'https://example.atlassian.net/.../1',
                     'events': list(command_module.WEBHOOK_EVENTS)}]
        raise AssertionError('no write may be made when the webhook already has every event')

    monkeypatch.setattr(command_module, '_jira_request', fake_request)

    output = _run_command()

    assert 'already registered' in output
    assert calls == ['GET']


def test_an_existing_registration_missing_an_event_is_updated_not_returned_from(monkeypatch):
    """canonical-delivery-state.md REQ-09, "Jira's comment and link events
    reach `core`" — up to v5.1 this returned on a URL match alone, so a
    deployment registered before `comment_created` joined WEBHOOK_EVENTS
    stayed subscribed to the old set for ever, and `webhook_consumer.py`'s
    comment branch was never reached. The registration is now PUT to its own
    `self` link with the full event list."""
    _set_required_env(monkeypatch)
    calls = []
    self_link = 'https://example.atlassian.net/rest/webhooks/1.0/webhook/7'

    def fake_request(method, url, email, token, body=None):
        calls.append((method, url, body))
        if method == 'GET':
            return [{'url': WEBHOOK_URL, 'self': self_link,
                     'events': ['jira:issue_created', 'jira:issue_updated']}]
        return {'self': self_link}

    monkeypatch.setattr(command_module, '_jira_request', fake_request)

    output = _run_command()

    assert [c[0] for c in calls] == ['GET', 'PUT']
    assert calls[1][1] == self_link, 'the existing registration is addressed by its own self link'
    assert calls[1][2]['events'] == command_module.WEBHOOK_EVENTS
    assert 'comment_created' in calls[1][2]['events']
    assert 'comment_updated' not in calls[1][2]['events'], \
        'core records a comment once, keyed on its Jira id; an edit is not mirrored'
    assert 'updated' in output


def test_the_subscribed_event_list_is_what_the_webhook_consumer_dispatches_on():
    """REQ-09, REQ-11 — the registration and the consumer have to agree: an
    event `webhook_consumer.handle_webhook_envelope` branches on but nobody
    subscribes is a branch that never runs, which is exactly what
    `comment_created` was before v5.2 and `issuelink_created` would be
    without REQ-11's addition."""
    assert command_module.WEBHOOK_EVENTS == [
        'jira:issue_created', 'jira:issue_updated', 'comment_created', 'issuelink_created',
    ]


def test_init_projects_manual_registration_message_lists_every_subscribed_event():
    """canonical-delivery-state.md §5's REQ-09/REQ-11 line —
    "`init-project.sh:284`'s manual-registration message lists every
    `WEBHOOK_EVENTS` entry". That message is what an operator follows when
    the command could not reach Jira, so a list that has fallen behind this
    one produces a registration Jira mode does not work with, and nothing
    would say so."""
    script = (pathlib.Path(__file__).resolve().parents[3] / 'scripts' / 'init-project.sh').read_text()
    events_lines = [line for line in script.splitlines() if 'Events:' in line]
    assert len(events_lines) == 1, 'one manual-registration message, so one list to keep in step'
    listed = [event.strip() for event in events_lines[0].split('Events:')[1].rstrip('"').split(',')]
    assert listed == command_module.WEBHOOK_EVENTS


def test_registration_is_attempted_even_when_the_existence_check_fails(monkeypatch):
    """Mirrors the deleted bash ensure_webhook()'s `|| true` around its GET:
    an unreadable existing-webhook list must not block registration."""
    _set_required_env(monkeypatch)
    calls = []

    def fake_request(method, url, email, token, body=None):
        calls.append(method)
        if method == 'GET':
            raise OSError('network unreachable')
        return {'self': 'https://example.atlassian.net/rest/webhooks/1.0/webhook/1'}

    monkeypatch.setattr(command_module, '_jira_request', fake_request)

    output = _run_command()

    assert 'Webhook registered' in output
    assert calls == ['GET', 'POST']


def test_jira_error_response_fails_the_command_with_the_reason(monkeypatch):
    _set_required_env(monkeypatch)

    def fake_request(method, url, email, token, body=None):
        if method == 'GET':
            return []
        return {'errorMessages': ['you do not have permission to add webhooks']}

    monkeypatch.setattr(command_module, '_jira_request', fake_request)

    with pytest.raises(CommandError, match='you do not have permission to add webhooks'):
        _run_command()


def test_network_failure_on_post_fails_the_command(monkeypatch):
    _set_required_env(monkeypatch)

    def fake_request(method, url, email, token, body=None):
        if method == 'GET':
            return []
        raise OSError('connection refused')

    monkeypatch.setattr(command_module, '_jira_request', fake_request)

    with pytest.raises(CommandError, match='connection refused'):
        _run_command()


@pytest.mark.parametrize('missing', ['JIRA_URL', 'JIRA_EMAIL', 'JIRA_TOKEN', 'HQ_URL'])
def test_missing_jira_or_hq_config_fails_before_any_network_call(monkeypatch, missing):
    _set_required_env(monkeypatch, **{missing: None})

    def fake_request(*args, **kwargs):
        raise AssertionError('must not call Jira when required config is missing')

    monkeypatch.setattr(command_module, '_jira_request', fake_request)

    with pytest.raises(CommandError, match=missing):
        _run_command()


def test_missing_webhook_secret_fails_before_any_network_call(monkeypatch):
    """WEBHOOK_SECRET is this service's own credential (workitems/views.py
    authenticates inbound webhooks against it) — never gathered by
    scripts/init-project.sh, so a misconfigured deployment must fail loudly
    rather than register a URL nothing can authenticate."""
    _set_required_env(monkeypatch, WEBHOOK_SECRET=None)

    def fake_request(*args, **kwargs):
        raise AssertionError('must not call Jira when WEBHOOK_SECRET is missing')

    monkeypatch.setattr(command_module, '_jira_request', fake_request)

    with pytest.raises(CommandError, match='WEBHOOK_SECRET'):
        _run_command()
