"""
Registers this deployment's Jira webhook, idempotently — the inbound half
of the ownership boundary BUGFIXES.md BF-02 (V4.1 line, carried to V5)
restores: Jira-protocol operations — constructing a Jira REST URL, calling
the Jira API, parsing its response — live beside workitems/jira_interpret.py's
payload interpretation, whose own module docstring already states that
"platform-specific interpretation of a Jira webhook payload lives entirely
here (and in webhook_consumer.py ...), never in ScrumMaster or any other
Streams client." BF-02 held that a shell script speaking the Jira REST API
was itself the defect, so scripts/init-project.sh's old `ensure_webhook()`
(a curl POST to Jira's legacy webhooks endpoint) is deleted rather than
repointed, and this command is where that operation now lives.

Invoked from init-project.sh's --connect-jira path via
`docker exec --env-file <tmp> core-api python manage.py ensure_jira_webhook`
— the same env-file-not-argv pattern scripts/startup/create-admin.sh
already uses for AIGANG_ADMIN_* to keep a credential out of the host
process table. init-project.sh still gathers JIRA_URL/JIRA_EMAIL/JIRA_TOKEN
and HQ_URL from the platform .env (BUGFIXES.md BF-02 point 5's provisioning
exception, confirmed unrenamed by strategy/v5.0/READINESS_DECISIONS.md item 7
ruling 1) and hands them to this one process's environment for the life of
this one call; it never constructs the Jira URL or reads Jira's response
itself.

WEBHOOK_SECRET is deliberately NOT one of the values init-project.sh hands
over: it is this service's own credential — workitems/views.py already
authenticates every inbound webhook against `os.environ['WEBHOOK_SECRET']`
— so this command reads it from its own container environment
(services/core/.env), the same way it reads everything else Django-side.
Nothing yet derives WEBHOOK_SECRET into services/core/.env from the
platform .env (scripts/startup/derive-env.sh does this for every
JIRA_*_FIELD_ID but not for WEBHOOK_SECRET) — an operator must set it there
directly today. That gap predates this command (the old ensure_webhook()
wrote a generated secret into services/scrummaster/.env, a file core-api's
docker-compose.yml never reads) and is out of BF-02's stated scope; see
this track's final report for the proposal to close it.

Registers `${HQ_URL}/webhooks/jira?secret=<WEBHOOK_SECRET>` — the live
route (workitems/urls.py), plural, replacing the deleted singular
`/webhook/jira` — via Jira's legacy `/rest/webhooks/1.0/webhook` endpoint
(works with basic auth; the same one the deleted ensure_webhook() used).

The Jira HTTP call is isolated in `_jira_request`, the only place this
module touches the network, so tests replace it wholesale and never make a
real request.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.request
from typing import Any

from django.core.management.base import BaseCommand, CommandError

WEBHOOK_NAME = 'AI Gang'
WEBHOOK_EVENTS = ['jira:issue_created', 'jira:issue_updated']


class JiraWebhookError(RuntimeError):
    """Registration itself failed or Jira's response didn't look like
    success. The one exception type this module's callers need to catch —
    a raw urllib/network exception never escapes `ensure_jira_webhook`."""


def _jira_request(method: str, url: str, email: str, token: str, body: dict[str, Any] | None = None) -> Any:
    """One Jira REST call — GET the existing webhook list or POST a new
    one. Returns the parsed JSON body. The only network-touching function
    in this module; tests monkeypatch this name wholesale."""
    data = json.dumps(body).encode('utf-8') if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header('Accept', 'application/json')
    if data is not None:
        request.add_header('Content-Type', 'application/json')
    credentials = base64.b64encode(f'{email}:{token}'.encode('utf-8')).decode('ascii')
    request.add_header('Authorization', f'Basic {credentials}')
    with urllib.request.urlopen(request) as response:
        raw = response.read()
    return json.loads(raw) if raw else None


def ensure_jira_webhook(*, jira_url: str, jira_email: str, jira_token: str, webhook_url: str) -> bool:
    """Registers `webhook_url` with Jira unless it is already registered.
    Returns True if this call registered it, False if it was already
    present. Raises JiraWebhookError if registration was attempted and
    failed. A failure to read the existing list is tolerated — the same
    best-effort behavior the deleted bash ensure_webhook() had (`|| true`
    around its GET) — and registration is attempted anyway."""
    endpoint = f'{jira_url}/rest/webhooks/1.0/webhook'

    try:
        existing = _jira_request('GET', endpoint, jira_email, jira_token)
    except Exception:
        existing = None

    for hook in existing if isinstance(existing, list) else []:
        if isinstance(hook, dict) and hook.get('url') == webhook_url:
            return False

    try:
        result = _jira_request('POST', endpoint, jira_email, jira_token, {
            'name': WEBHOOK_NAME,
            'url': webhook_url,
            'events': WEBHOOK_EVENTS,
            'filters': {},
            'excludeBody': False,
        })
    except Exception as err:
        raise JiraWebhookError(str(err)) from err

    if not (isinstance(result, dict) and result.get('self')):
        reason = 'unknown error'
        if isinstance(result, dict):
            messages = result.get('messages') or []
            errors = result.get('errorMessages') or []
            if messages and isinstance(messages[0], dict) and messages[0].get('arguments'):
                reason = messages[0]['arguments'][0]
            elif errors:
                reason = errors[0]
        raise JiraWebhookError(reason)

    return True


class Command(BaseCommand):
    help = "Register this deployment's Jira webhook at /webhooks/jira, idempotently (BUGFIXES.md BF-02)."

    def handle(self, *args, **options):
        jira_url = (os.environ.get('JIRA_URL') or '').rstrip('/')
        jira_email = os.environ.get('JIRA_EMAIL') or ''
        jira_token = os.environ.get('JIRA_TOKEN') or ''
        hq_url = (os.environ.get('HQ_URL') or '').rstrip('/')
        webhook_secret = os.environ.get('WEBHOOK_SECRET') or ''

        missing = [
            name for name, value in (
                ('JIRA_URL', jira_url),
                ('JIRA_EMAIL', jira_email),
                ('JIRA_TOKEN', jira_token),
                ('HQ_URL', hq_url),
            ) if not value
        ]
        if missing:
            raise CommandError(
                'missing required environment variable(s): ' + ', '.join(missing)
            )
        if not webhook_secret:
            raise CommandError(
                "WEBHOOK_SECRET is not set in this service's own environment "
                '(services/core/.env) — set it there so POST /webhooks/jira '
                'can authenticate the request this command is about to register.'
            )

        webhook_url = f'{hq_url}/webhooks/jira?secret={webhook_secret}'

        try:
            registered = ensure_jira_webhook(
                jira_url=jira_url,
                jira_email=jira_email,
                jira_token=jira_token,
                webhook_url=webhook_url,
            )
        except JiraWebhookError as err:
            raise CommandError(f'Jira webhook registration failed: {err}') from err

        if registered:
            self.stdout.write(f'Webhook registered: {webhook_url}')
        else:
            self.stdout.write('Webhook already registered — skipping.')
