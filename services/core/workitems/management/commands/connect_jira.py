"""
`connect_jira <project> <jira-key>` — canonical-delivery-state.md REQ-10.

**The only path that puts a project into Jira mode.** Up to v5.1 that was
`catchup.connect_jira`, which flipped the mode and nothing else: the
project's existing work items stayed invisible to Jira, and every write for
them was routed at a tracker that had never heard of them. This command is
the push v5.1 removed, rebuilt inside `core` now that `core` holds the
platform's only Jira client.

It is run in `core`'s environment, which holds that client's credentials,
**with the project quiesced**: the operator stops ScrumMaster before
running it, starts it after it exits, and makes no admin or API change to
the project meanwhile. The command does not detect a running ScrumMaster.

What it does, in order:

1. **Refuses, changing nothing**, unless the Jira credentials and all
   fourteen field ids are set, non-blank and not placeholders; unless Jira
   lists a webhook at this service's own path carrying every
   `WEBHOOK_EVENTS` entry; and while any `release` work item of the project
   is open. A project already in Jira mode is refused by
   `project_config.set_jira_project_key` itself, so this command reads no
   mode (REQ-09, "Where the mode is read is a property, stated as a
   search"); re-syncing one means running `disconnect_jira` and then this.
2. **Records the Jira project key with the project still local**, so a push
   that fails part way leaves every key it did record where a re-run will
   find it, while `core` still ignores the project's Jira webhooks (v5.1's
   REQ-10).
3. **Pushes**, parents before children: an item with no `external_key` gets
   a Jira issue — a top-level one by its canonical type, an item with a
   parent as a Sub-task under its parent's issue — with its Agent field
   and, for a story, its story fields set in the same request. Each create
   and its `record_external_key` commit before the next item's create.
4. **Re-syncs** every item but a release: the Jira status its canonical
   status maps to, the Blocked flag for `needs-clarification`, `failed` or
   `cancelled`, and a Blocks link for each canonical `blocks` link whose
   two ends both have keys.
5. **Switches**, but only when every item but releases has a key and no
   re-sync step failed in this run. The writer's consumer group on the
   project's event stream moves to the stream's end first, so nothing the
   project published while it was local is written to Jira.

**A `release` is neither pushed nor re-synced** (Pass 7, answer 2.1): its
`jira:issue_created` would publish `requested` and cut a candidate, and a
re-sync move to Done whose webhook `core` processes after the switch would
publish `done` and promote it (REQ-08).

**The re-sync is the writer's status rules with REQ-10's own outcomes**, and
`connect_jira` is REQ-09's one exception to "the writer makes no Jira write
for a project not then in Jira mode" — it writes to Jira for a project that
is still local. Two outcomes differ from a writer push, both deliberately:

  - a canonical status the project's Jira status map does not cover
    (`assigned` or `waiting-on-dependency` with the default map) leaves the
    issue as Jira has it. That is neither a write, a rejection nor a failed
    step (Pass 7, answer 1) — the opposite of the inbound webhook rule,
    which records a failure for an unmapped status;
  - an issue already in the target status, or in another Jira status
    mapping to the same canonical status, counts as DONE whether or not its
    Blocked flag is set, because the re-sync clears no flag.
"""

from __future__ import annotations

import os
from typing import Optional

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from workitems import (
    catchup, jira_client, jira_writer, project_config, status_vocabulary,
)
from workitems.models import WorkItem, WorkItemLink, WorkItemStoryDetail

from .ensure_jira_webhook import WEBHOOK_EVENTS, list_webhooks, webhook_missing_events

# The platform-level Jira credentials, and every field id in
# `scripts/startup/derive-env.sh`'s `JIRA_FIELD_ID_VARS` loop, which is what
# carries them into this service's own environment. REQ-10 refuses to
# connect a project unless all of them are set, non-blank and not one of
# `jira_client.ENV_PLACEHOLDERS`: a missing field id is not a runtime error
# in this command, it is a Jira write that silently drops a field months
# later. `tests/test_connect_jira_command.py` pins this list against
# derive-env.sh's own array, so the two cannot drift.
JIRA_CREDENTIAL_VARS = ('JIRA_URL', 'JIRA_EMAIL', 'JIRA_TOKEN')

JIRA_FIELD_ID_VARS = (
    'JIRA_AGENT_FIELD_ID',
    'JIRA_BLOCKED_FIELD_ID',
    'JIRA_VALUE_HYPOTHESIS_FIELD_ID',
    'JIRA_TEST_MEASUREMENT_FIELD_ID',
    'JIRA_BEHAVIOR_FIELD_ID',
    'JIRA_AC_FIELD_ID',
    'JIRA_CONSTRAINTS_FIELD_ID',
    'JIRA_EDGE_CASES_FIELD_ID',
    'JIRA_OUT_OF_SCOPE_FIELD_ID',
    'JIRA_TARGET_PROJECT_FIELD_ID',
    'JIRA_RELEASE_NOTES_FIELD_ID',
    'JIRA_CANDIDATE_SHA_FIELD_ID',
    'JIRA_BUILD_IDENTIFIER_FIELD_ID',
    'JIRA_PREVIEW_URL_FIELD_ID',
)

# The story fields a pushed Story carries, as (canonical detail column, the
# env var holding its Jira field id) — `jira_interpret.parse_story_fields`'s
# own set, read in the other direction.
STORY_FIELD_VARS = (
    ('value_hypothesis', 'JIRA_VALUE_HYPOTHESIS_FIELD_ID'),
    ('test_measurement', 'JIRA_TEST_MEASUREMENT_FIELD_ID'),
    ('behavior', 'JIRA_BEHAVIOR_FIELD_ID'),
    ('acceptance_criteria', 'JIRA_AC_FIELD_ID'),
    ('constraints', 'JIRA_CONSTRAINTS_FIELD_ID'),
    ('edge_cases', 'JIRA_EDGE_CASES_FIELD_ID'),
    ('out_of_scope', 'JIRA_OUT_OF_SCOPE_FIELD_ID'),
)

# `jiraCatchupConsumer.js`'s own `issueTypeFor` (`e39e9ab`, `:27-38`), which
# is what mapped a canonical type to a Jira issue type the last time this
# push existed. The canonical `type` is a vocabulary, not an exhaustive
# enum, so an unrecognised type falls back to a capitalised guess rather
# than failing the push.
ISSUE_TYPE_BY_CANONICAL_TYPE = {'story': 'Story', 'task': 'Task'}

OPEN_RELEASE_BASELINES = ('done', 'cancelled', 'failed')

STEP_CREATE = 'connect-jira-create'
STEP_STATUS = 'connect-jira-status'
STEP_BLOCKED_FLAG = 'connect-jira-blocked-flag'
STEP_LINK = 'connect-jira-link'


def issue_type_for(canonical_type: Optional[str]) -> str:
    if canonical_type in ISSUE_TYPE_BY_CANONICAL_TYPE:
        return ISSUE_TYPE_BY_CANONICAL_TYPE[canonical_type]
    return canonical_type[:1].upper() + canonical_type[1:] if canonical_type else 'Task'


def missing_environment() -> list[str]:
    """Every required variable that is unset, blank or still holding one of
    the shipped template's placeholder values."""
    missing = []
    for name in (*JIRA_CREDENTIAL_VARS, *JIRA_FIELD_ID_VARS):
        value = (os.environ.get(name) or '').strip()
        if not value or value in jira_client.ENV_PLACEHOLDERS:
            missing.append(name)
    return missing


def open_releases(project: str) -> list[WorkItem]:
    """Every `release` work item of the project that is still open — "its
    baseline not `done`, `cancelled` or `failed`".

    Both commands refuse while one exists, and for the same reason: while
    the project is local `core` ignores the Release's Jira webhooks, so
    `connect_jira` would otherwise refuse until a person closed the Release
    in the admin, which publishes a release event (REQ-08)."""
    custom_statuses = project_config.get_custom_statuses(project)
    return [
        item for item in WorkItem.objects.filter(project=project, type='release')
        if status_vocabulary.baseline_of(item.status, custom_statuses) not in OPEN_RELEASE_BASELINES
    ]


def _pushable_items(project: str) -> list[WorkItem]:
    """Every work item of the project but a `release`, parents before
    children, so an item with a parent always finds its parent's issue key
    already recorded."""
    items = list(WorkItem.objects.filter(project=project).exclude(type='release').order_by('created_at'))
    by_id = {str(item.id): item for item in items}

    def depth(item: WorkItem, seen: Optional[set] = None) -> int:
        seen = seen or set()
        parent_id = str(item.parent_id) if item.parent_id else None
        if not parent_id or parent_id in seen or parent_id not in by_id:
            return 0
        seen.add(parent_id)
        return 1 + depth(by_id[parent_id], seen)

    return sorted(items, key=depth)


def _story_detail_fields(item: WorkItem) -> dict:
    """A story's own fields, as Atlassian Document Format paragraphs — the
    shape `jira_interpret` reads back with `adf_to_text` and REST API v3
    requires for a multi-line field (REQ-10)."""
    detail = WorkItemStoryDetail.objects.filter(work_item_id=item.id).first()
    if detail is None:
        return {}
    fields = {}
    for column, env_name in STORY_FIELD_VARS:
        field_id = os.environ.get(env_name)
        value = getattr(detail, column, None)
        if field_id and value:
            fields[field_id] = jira_client.adf_document(value)
    return fields


def _create_fields(item: WorkItem) -> dict:
    fields = {}
    agent_field_id = os.environ.get('JIRA_AGENT_FIELD_ID')
    if agent_field_id and item.assignee_agent_id:
        fields[agent_field_id] = {'value': item.assignee_agent_id}
    if item.type == 'story':
        fields.update(_story_detail_fields(item))
    return fields


class _Failures:
    """Every failed create or re-sync step, each recorded as a webhook
    failure naming the item and the step the moment it happens (Pass 6,
    SR-6-26), and collected so the command can name them all on the way out
    and withhold the switch."""

    def __init__(self):
        self.entries: list[str] = []

    def record(self, item: WorkItem, step: str, reason: str, detail: Optional[dict] = None) -> None:
        jira_writer.record_webhook_failure(item, f'connect_jira: {reason}', {'step': step, **(detail or {})})
        self.entries.append(f'{item.display_name} ({item.id}) — {step}: {reason}')

    def __bool__(self) -> bool:
        return bool(self.entries)


def push_project(project: str, jira_project_key: str, *, stdout=None) -> _Failures:
    """Steps 3 and 4: the push and the re-sync. Returns the failures, so the
    caller can withhold the switch while any exists."""
    failures = _Failures()

    def say(line: str) -> None:
        if stdout is not None:
            stdout.write(line)

    items = _pushable_items(project)

    # --- 3. the push: an issue for every item with no key -----------------
    for item in items:
        if item.external_key:
            continue
        parent = WorkItem.objects.filter(id=item.parent_id).first() if item.parent_id else None
        try:
            if parent is not None and parent.external_key:
                issue_key = jira_client.create_subtask(
                    parent.external_key, jira_project_key, item.display_name,
                    item.description or item.display_name, item.assignee_agent_id,
                )
            else:
                issue_key = jira_client.create_issue(
                    jira_project_key, issue_type_for(item.type), item.display_name,
                    item.description, _create_fields(item),
                )
        except Exception as err:  # noqa: BLE001 - recorded as a failed step, naming the item
            failures.record(item, STEP_CREATE, f'creating a Jira issue failed: {err}')
            continue
        # Committed before the next item's create, so a failure part way
        # through leaves every earlier key recorded and a re-run resumes.
        catchup.record_external_key(item.id, issue_key, actor='connect-jira')
        item.external_key = issue_key
        say(f'  created {issue_key} for {item.display_name} ({item.id})')

    # --- 4. the re-sync: status, flag and Blocks links --------------------
    for item in items:
        if not item.external_key:
            continue  # its create failed in this run; nothing to re-sync.
        _resync_status(project, item, failures, say)

    _resync_links(project, items, failures, say)
    return failures


def _resync_status(project: str, item: WorkItem, failures: _Failures, say) -> None:
    if item.status in jira_writer.BLOCKED_FLAG_STATUSES:
        # Jira shows all three as one Blocked flag (REQ-09). No transition,
        # and no flag is ever CLEARED by a re-sync (Pass 7, answer 1).
        try:
            jira_client.set_blocked_field(item.external_key, True)
        except Exception as err:  # noqa: BLE001
            failures.record(item, STEP_BLOCKED_FLAG, f'setting the Blocked flag failed: {err}')
            return
        say(f'  flagged {item.external_key} as Blocked ({item.status})')
        return

    jira_status = jira_writer.canonical_to_jira_status(project, item.status)
    if jira_status is None:
        # "left as Jira has it, which is neither a write, a rejection nor a
        # failed step" (REQ-10; §4's "Re-syncing an item whose canonical
        # status no Jira status maps to" row).
        say(f'  left {item.external_key} as Jira has it — no Jira status maps to "{item.status}"')
        return

    try:
        outcome = jira_writer.transition_outcome(project, item.external_key, jira_status)
    except Exception as err:  # noqa: BLE001
        failures.record(item, STEP_STATUS, f'transitioning to "{jira_status}" failed: {err}',
                         {'targetJiraStatus': jira_status})
        return

    if outcome['transitioned']:
        say(f'  moved {item.external_key} to "{jira_status}"')
        return
    if outcome['alreadyMapped']:
        # Counted done whether or not the Blocked flag is set, unlike a
        # writer push, because the re-sync clears no flag (REQ-10).
        say(f'  {item.external_key} is already in "{outcome["currentJiraStatus"]}", '
            f'which maps to "{item.status}" — counted done')
        return
    failures.record(
        item, STEP_STATUS,
        f'Jira issue {item.external_key} offers no transition to "{jira_status}" and is in '
        f'"{outcome["currentJiraStatus"]}", which maps to no canonical status for this project',
        {'targetJiraStatus': jira_status, 'currentJiraStatus': outcome['currentJiraStatus']},
    )


def _resync_links(project: str, items: list[WorkItem], failures: _Failures, say) -> None:
    """"a Blocks link is created, unless `get_issue_links` already shows it,
    for each canonical `blocks` link whose two ends both have keys"
    (REQ-10)."""
    keyed = {str(item.id): item for item in items if item.external_key}
    links = WorkItemLink.objects.filter(
        link_type='blocks', from_work_item__project=project, to_work_item__project=project,
    ).values_list('from_work_item_id', 'to_work_item_id')

    for blocker_id, dependent_id in links:
        blocker = keyed.get(str(blocker_id))
        dependent = keyed.get(str(dependent_id))
        if blocker is None or dependent is None:
            continue  # a release, or an end whose create failed: both ends must have keys.
        try:
            existing = jira_client.get_issue_links(dependent.external_key)
            if blocker.external_key in (existing.get('isBlockedBy') or []):
                continue
            jira_client.create_issue_link(blocker.external_key, dependent.external_key)
        except Exception as err:  # noqa: BLE001
            failures.record(dependent, STEP_LINK,
                             f'creating the Blocks link {blocker.external_key} -> '
                             f'{dependent.external_key} failed: {err}',
                             {'blockerKey': blocker.external_key})
            continue
        say(f'  linked {blocker.external_key} blocks {dependent.external_key}')


class Command(BaseCommand):
    help = (
        'Push a project\'s work items into Jira and switch it to Jira mode '
        '(canonical-delivery-state.md REQ-10). Run with ScrumMaster stopped and the project '
        'otherwise left alone.'
    )

    def add_arguments(self, parser):
        parser.add_argument('project', help="the AI Gang project name")
        parser.add_argument('jira_project_key', help="the Jira project key its issues are created in")

    def handle(self, *args, **options):
        project = options['project']
        jira_project_key = options['jira_project_key']

        # --- 1. the refusals, changing nothing ----------------------------
        missing = missing_environment()
        if missing:
            raise CommandError(
                'refusing to connect: these variables are unset, blank or still a placeholder — '
                + ', '.join(missing)
                + '. Run scripts/create-jira-fields.sh and scripts/create-release-fields.sh, then '
                're-run platform startup so derive-env.sh carries the ids into this service.'
            )

        jira_url = (os.environ.get('JIRA_URL') or '').rstrip('/')
        missing_events = webhook_missing_events(list_webhooks(
            jira_url=jira_url,
            jira_email=os.environ.get('JIRA_EMAIL') or '',
            jira_token=os.environ.get('JIRA_TOKEN') or '',
        ))
        if missing_events:
            raise CommandError(
                'refusing to connect: no Jira webhook registered at /webhooks/jira carries every '
                'event this service needs — missing ' + ', '.join(missing_events)
                + '. Run `python manage.py ensure_jira_webhook` (it updates an existing '
                'registration), then re-run this command. Jira mode depends on these: '
                + ', '.join(WEBHOOK_EVENTS)
            )

        open_release_items = open_releases(project)
        if open_release_items:
            raise CommandError(
                'refusing to connect: these release work items are still open — '
                + ', '.join(f'{item.display_name} ({item.id}, {item.status})' for item in open_release_items)
                + '. A release is neither pushed nor re-synced, and a Release that changes hands '
                'mid-connect would cut or promote a candidate.'
            )

        # --- 2. the key, with the project still local ---------------------
        try:
            project_config.set_jira_project_key(project, jira_project_key)
        except project_config.AlreadyJiraModeError as err:
            raise CommandError(f'refusing to connect: {err}') from err

        # --- 3 and 4. the push and the re-sync ----------------------------
        self.stdout.write(f'Pushing {project} into Jira project {jira_project_key}...')
        failures = push_project(project, jira_project_key, stdout=self.stdout)

        if failures:
            raise CommandError(
                f'{project} is still in LOCAL mode: '
                f'{len(failures.entries)} item(s) failed, each recorded as a webhook failure —\n  '
                + '\n  '.join(failures.entries)
                + '\nFix the cause and re-run; every item that already has a key is skipped.'
            )

        # --- 5. the switch ------------------------------------------------
        # The group moves BEFORE the switch, and both after the push: the
        # push's own events (each `record_external_key`) are published while
        # the project is still local, and moving the group to the stream's
        # end here is what excludes them, and everything the project
        # published in local mode before them, from the writer. A failure
        # here leaves the project local, which is this command's own rule
        # for any failed step.
        jira_writer.move_writer_group_to_stream_end(project)
        with transaction.atomic():
            project_config.set_mode(project, project_config.JIRA, jira_project_key=jira_project_key)

        self.stdout.write(self.style.SUCCESS(
            f'{project} is now in Jira mode on Jira project {jira_project_key}, and the writer\'s '
            "consumer group starts at its event stream's end. Start ScrumMaster again."
        ))
