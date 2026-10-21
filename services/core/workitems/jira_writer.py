"""
`core`'s outbound Jira writer (canonical-delivery-state.md REQ-09).

Every Jira write the platform makes on a work item's behalf is made here,
through v5.1's client (`jira_client.py`), and only for a project that is in
Jira mode at the moment the input is handled. It has two inputs:

  - **in-process calls from the routing layer** — `store.py`'s comment path
    and its status, assignment and link handlers, and (REQ-11, built by a
    later track) `materialize.py`'s decomposition. Each handler calls
    `write_gate.route` first and reaches this module only on `push`;
  - **the project's canonical event stream**,
    `aigang:workitems:{project}:events`, for the events that have a Jira
    side effect (a recorded release candidate, a story-intake decision).
    Its consumer group is created at the stream's END, unlike
    `streams.ensure_group`'s default of its start, so nothing published
    before the writer existed is written (v5.1 audit SR-5-02).

`write_gate.mode_of` is the only way this module reads a project's mode, and
`write_gate.route` is the only thing that decides whether a write reaches
here at all — no caller, command payload or event carries a mode.

**Nothing recorded, ever.** A pushed change reaches the canonical store only
through Jira's own webhook, so `canonical-work-model.md` REQ-10's single
write path in Jira mode is unchanged. The one thing this module writes to
the database is bookkeeping: its per-step completion records
(`JiraWriteCompletion`) and the operator-visible `WebhookFailure` rows that
record an outcome rather than retry it.

**No retry policy of its own** (§4). A failed Jira call on a path that can
still fail its caller's message propagates, and the stream's existing retry
and dead-letter handle it; a call registered with `transaction.on_commit`
cannot fail its caller's message, so its failure is recorded as one webhook
failure and not retried. The per-call 30-second timeout this stage sets on
the client (`jira_client.TIMEOUT_SECONDS`) is a transient failure either
way.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from django.db import connection, transaction
from django.utils import timezone

from . import jira_client, project_config, write_gate
from .models import (
    JiraDecompositionProposal, JiraWriteCompletion, ProjectConfig, ProjectStatusConfig, WebhookFailure,
    WorkItem,
)

logger = logging.getLogger(__name__)

# The canonical statuses Jira shows as one Blocked flag rather than as a
# status of their own (v5.2 REQ-09, "A status write to `needs-clarification`,
# `failed` or `cancelled`"). §4 records the cost: in Jira mode `failed` is
# read back as `needs-clarification`. From v5.3 `cancelled` is not one of
# them: it is written as the Jira status the project's map writes for it —
# Abandoned with `DEFAULT_JIRA_STATUS_MAP` — for every issue type
# (release-mode-parity.md REQ-14).
BLOCKED_FLAG_STATUSES = ('needs-clarification', 'failed')

# Step names used in completion records and webhook-failure reasons. One per
# kind of Jira write, so a redelivery can skip exactly the step that already
# succeeded and an operator reading a failure knows which call it was.
STEP_COMMENT = 'comment'
STEP_STATUS = 'status'
STEP_ASSIGNMENT = 'assignment'
STEP_LINK = 'link'
STEP_BLOCKED_FLAG = 'blocked-flag'
STEP_RELEASE_FIELDS = 'release-fields'
STEP_AGENT_FIELD = 'agent-field'


class JiraWriteRejectedError(Exception):
    """A push this writer refuses on a validation of its own, before any
    Jira call: a status write to a canonical status the project's status map
    does not cover (REQ-09, "A status write to any other status → rejected
    as a validation"). Carries `VALIDATION_ERROR` so
    `command_consumer.is_permanent_rejection` dead-letters it once and
    `command_consumer`'s rejection comment covers it, exactly as a
    `store.ValidationError` is."""
    code = 'VALIDATION_ERROR'


# ---------------------------------------------------------------------------
# The status map, per project (REQ-09, "A status write to …"; Shovel Ready
# Pass 8, answer 1.1)
# ---------------------------------------------------------------------------

def canonical_to_jira_status(project: str, canonical_status: str) -> Optional[str]:
    """The inverse of `webhook_consumer.py`'s inbound map, chosen per
    project in the order `canonical-work-model.md` REQ-11's resolution
    records: a project with `ProjectStatusConfig` rows is mapped by its rows
    ALONE; `DEFAULT_JIRA_STATUS_MAP` maps a project with none.

    Returns None when the project's map does not cover `canonical_status` —
    including a status only `DEFAULT_JIRA_STATUS_MAP` maps, for a project
    that has rows. That is "unmapped", and a status write to it is rejected
    as a validation.

    Ties: a canonical status several of a project's rows map to is written
    as the LOWEST-ID row's Jira status; one several `DEFAULT_JIRA_STATUS_MAP`
    entries map to, for a project with no rows, as the FIRST such entry."""
    from .webhook_consumer import DEFAULT_JIRA_STATUS_MAP

    rows = list(ProjectStatusConfig.objects.filter(project=project).order_by('id'))
    if rows:
        for row in rows:
            if row.status == canonical_status and row.jira_status_name:
                return row.jira_status_name
        return None

    for jira_status_name, mapped in DEFAULT_JIRA_STATUS_MAP.items():
        if mapped == canonical_status:
            return jira_status_name
    return None


def jira_status_to_canonical(project: str, jira_status_name: str) -> Optional[str]:
    """What `jira_status_name` means canonically for this project, or None
    when the project's map does not name it.

    **This is `webhook_consumer.jira_status_to_canonical`** — the one
    inbound map, which `canonical_to_jira_status` above inverts. Delegated
    rather than reimplemented: the writer and the webhook consumer have to
    agree on which map a project uses and on what an unnamed status means,
    and the only way to guarantee that is for there to be one map. (This
    module keeps the thin alias so a reader of the writer's
    missing-transition check does not have to know which module the
    forward direction lives in.)

    Imported inside the function because `webhook_consumer` imports
    `store`, which imports this module — the same lazy import
    `canonical_to_jira_status` already makes for `DEFAULT_JIRA_STATUS_MAP`."""
    from .webhook_consumer import jira_status_to_canonical as inbound_map

    return inbound_map(project, jira_status_name)


# ---------------------------------------------------------------------------
# Completion records (REQ-09, "Redelivery")
# ---------------------------------------------------------------------------

def is_step_complete(completion_key: Optional[str], work_item_id, step: str) -> bool:
    """A step with no key records no completion and is never skipped (a
    derived push, or the external API's transition). A *registered* step —
    `completed_at IS NULL` — is not complete either."""
    if not completion_key:
        return False
    return JiraWriteCompletion.objects.filter(
        completion_key=completion_key, work_item_id=work_item_id, step=step,
        completed_at__isnull=False,
    ).exists()


def register_step(completion_key: str, work_item_id, step: str) -> None:
    """Record that this message owes this step, WITHOUT marking it done.
    Story intake's comment steps are the one case registered ahead of their
    work (REQ-09, "Canonical events with a Jira side effect"), so
    `_handle_story_created`'s early return on a redelivery knows which
    comment steps its own `messageId` registered."""
    JiraWriteCompletion.objects.get_or_create(
        completion_key=completion_key, work_item_id=work_item_id, step=step,
    )


def mark_step_complete(completion_key: Optional[str], work_item_id, step: str) -> None:
    """Called only AFTER the Jira call succeeded. A key is never claimed
    before the work: a claim taken first turns a transient Jira failure
    into a lost write (REQ-09, "Redelivery")."""
    if not completion_key:
        return
    row, created = JiraWriteCompletion.objects.get_or_create(
        completion_key=completion_key, work_item_id=work_item_id, step=step,
        defaults={'completed_at': timezone.now()},
    )
    if not created and row.completed_at is None:
        row.completed_at = timezone.now()
        row.save(update_fields=['completed_at'])


def prune_completion_records(*, retention_days: int = JiraWriteCompletion.RETENTION_DAYS) -> int:
    """Kept at least as long as dead letters, so a replayed dead letter
    still finds its own record. Exposed for the retention command that
    trims the streams themselves."""
    from datetime import timedelta
    cutoff = timezone.now() - timedelta(days=retention_days)
    deleted, _ = JiraWriteCompletion.objects.filter(registered_at__lt=cutoff).delete()
    return deleted


# ---------------------------------------------------------------------------
# Failure recording
# ---------------------------------------------------------------------------

def record_webhook_failure(item: WorkItem, reason: str, detail: Optional[dict] = None) -> None:
    """The writer's one way of recording an outcome it will not retry.
    Writes the same operator-visible row `webhook_consumer.record_failure`
    writes; duplicated as a direct model write rather than imported, because
    `webhook_consumer` imports `store`, which imports this module."""
    WebhookFailure.objects.create(
        project=item.project, work_item_id=item.id, external_key=item.external_key,
        reason=reason, payload=detail or {}, occurred_at=timezone.now(),
    )


# ---------------------------------------------------------------------------
# Running a push: now, or after the caller's transaction commits
# ---------------------------------------------------------------------------

def _still_jira_mode(project: str) -> bool:
    """Read at the moment the write is made, not when it was routed: a
    deferred push runs after its caller committed, and the writer makes no
    Jira write for a project not then in Jira mode (REQ-09)."""
    return write_gate.mode_of(project) == project_config.JIRA


def _push(item: WorkItem, call, *, step: str, deferred_failure_reason: str):
    """Make one Jira write, or register it for after the caller's
    transaction commits.

    Called with no transaction open (a command consumer, the external API,
    a comment appended after its handler's block exits) the call runs now
    and its failure propagates: the stream redelivers the message, or the
    external API's caller receives the error.

    Called inside a caller's transaction (`connection.in_atomic_block`) the
    call is registered with `transaction.on_commit` and this returns at
    once, so no Jira call is ever made inside an open transaction and none
    is made at all if the caller rolls back. Such a push cannot fail its
    caller's message, so its failure is recorded as one webhook failure and
    is not retried (REQ-09, "Derived writes go through the router too"),
    and it raises nothing, so a later callback from the same commit still
    runs."""
    if connection.in_atomic_block:
        def deferred():
            try:
                call()
            except Exception as err:  # noqa: BLE001 - recorded, not retried; see the docstring
                logger.error('[jira-writer] %s for %s: %r', deferred_failure_reason, item.id, err)
                record_webhook_failure(item, f'{deferred_failure_reason}: {err}', {'step': step})
        transaction.on_commit(deferred)
        return {'posted': True, 'workItemId': str(item.id), 'deferred': True}

    call()
    return {'posted': True, 'workItemId': str(item.id), 'deferred': False}


def _no_issue_to_write(item: WorkItem, step: str) -> bool:
    """A Jira-mode work item with no `external_key` has no issue to write:
    one webhook failure naming it, nothing written, acknowledged
    (REQ-09, "Redelivery")."""
    if item.external_key:
        return False
    logger.error('[jira-writer] work item %s is in Jira mode with no external_key — nothing to write', item.id)
    record_webhook_failure(item, 'Jira-mode work item has no external_key — no issue to write', {'step': step})
    return True


# ---------------------------------------------------------------------------
# The Jira write for each input (REQ-09, "The writer's Jira write for each")
# ---------------------------------------------------------------------------

def transition_outcome(project: str, issue_key: str, jira_status: str) -> dict:
    """Make one transition and report WHAT HAPPENED, deciding nothing.

    `transition_issue` returns normally when the issue offers no transition
    to the named status (`jira_client.py`'s own skip-and-warn), so this
    distinguishes that case by its return value and, when it happens, reads
    the issue's current status and Blocked flag once (`get_issue`) so a
    caller can tell "the write it asked for has already taken effect" from
    "this workflow does not offer it".

    Returns `{'transitioned': bool, 'alreadyMapped': bool, 'flagged': bool,
    'currentJiraStatus': str|None}`. `alreadyMapped` is REQ-09's test: the
    issue is in a Jira status that maps to the SAME canonical status the
    target maps to (`jira_status_to_canonical`), which covers both the
    target status itself and another Jira status meaning the same thing.

    The decision is deliberately the caller's, because the two callers
    differ on exactly one point (REQ-10): a writer push counts an
    already-mapped issue done only while its Blocked flag is CLEAR, while
    `connect_jira`'s re-sync counts it done whether or not the flag is set,
    because the re-sync clears no flag."""
    if jira_client.transition_issue(issue_key, jira_status):
        return {'transitioned': True, 'alreadyMapped': False, 'flagged': False, 'currentJiraStatus': None}

    target_canonical = jira_status_to_canonical(project, jira_status)
    issue = jira_client.get_issue(issue_key)
    current = issue.get('status') or ''
    current_canonical = jira_status_to_canonical(project, current)
    return {
        'transitioned': False,
        'alreadyMapped': target_canonical is not None and current_canonical == target_canonical,
        'flagged': bool(issue.get('blocked')),
        'currentJiraStatus': current or None,
    }


def _transition_or_record_outcome(item: WorkItem, jira_status: str, *, completion_key: Optional[str],
                                   step: str) -> None:
    """The writer's rule over `transition_outcome` above: a missing
    transition is recorded as ONE webhook failure naming the work item, the
    target status and the step — the recorded outcome REQ-04 requires. It
    is never retried, and the step is recorded complete when the call
    carries a key.

    An issue already in a Jira status that maps to the target canonical
    status, with its Blocked flag CLEAR, is a SUCCESS — the write it asked
    for has taken effect (a duplicate derived push, or a person who moved
    the issue first). While the flag is set it is one webhook failure."""
    outcome = transition_outcome(item.project, item.external_key, jira_status)
    if outcome['transitioned']:
        mark_step_complete(completion_key, item.id, step)
        return

    if outcome['alreadyMapped'] and not outcome['flagged']:
        logger.info(
            '[jira-writer] %s offers no transition to "%s" but is already in a status that maps to it '
            '(%s) with its Blocked flag clear — counted done',
            item.external_key, jira_status, outcome['currentJiraStatus'],
        )
        mark_step_complete(completion_key, item.id, step)
        return

    record_webhook_failure(
        item,
        f'Jira issue {item.external_key} offers no transition to "{jira_status}"',
        {'step': step, 'targetJiraStatus': jira_status, 'currentJiraStatus': outcome['currentJiraStatus'],
         'blocked': outcome['flagged']},
    )
    mark_step_complete(completion_key, item.id, step)


def _status_call(item: WorkItem, canonical_status: str, completion_key: Optional[str], step: str):
    """The one function that turns a canonical status into a Jira write.
    Resolved at call time so a deferred push reads the project's map and
    mode when it runs, not when it was registered."""
    def call():
        if not _still_jira_mode(item.project):
            return
        if canonical_status in BLOCKED_FLAG_STATUSES:
            jira_client.set_blocked_field(item.external_key, True)
            mark_step_complete(completion_key, item.id, step)
            return
        jira_status = canonical_to_jira_status(item.project, canonical_status)
        if jira_status is None:
            raise JiraWriteRejectedError(
                f'this project\'s Jira status map writes no Jira status for "{canonical_status}", '
                f'so it cannot be written to Jira issue {item.external_key}'
            )
        _transition_or_record_outcome(item, jira_status, completion_key=completion_key, step=step)
    return call


def push_status(item: WorkItem, canonical_status: str, *, completion_key: Optional[str] = None,
                 step: str = STEP_STATUS) -> dict:
    """A routed status write (`store.transition_status` on `push`). An
    unmapped status is rejected as a validation BEFORE any Jira call, which
    is what makes it a `VALIDATION_ERROR` the command path dead-letters once
    with its single rejection comment."""
    if _no_issue_to_write(item, step):
        return {'posted': True, 'workItemId': str(item.id), 'skipped': 'no-external-key'}
    if is_step_complete(completion_key, item.id, step):
        return {'posted': True, 'workItemId': str(item.id), 'skipped': 'already-complete'}

    if canonical_status not in BLOCKED_FLAG_STATUSES and canonical_to_jira_status(item.project, canonical_status) is None:
        raise JiraWriteRejectedError(
            f'this project\'s Jira status map writes no Jira status for "{canonical_status}", '
            f'so it cannot be written to Jira issue {item.external_key}'
        )

    return _push(item, _status_call(item, canonical_status, completion_key, step),
                  step=step, deferred_failure_reason=f'pushing status "{canonical_status}" to Jira failed')


def register_derived_status_push(item: WorkItem, canonical_status: str) -> None:
    """A derived write's push — the parent rollup's `done` and the
    dependent unblock's `ready` (REQ-09, "Derived writes go through the
    router too"). It carries no completion key: it runs after the webhook's
    transaction commits and is recorded, not redelivered.

    For a project whose status map writes no Jira status for the target,
    which V2 REQ-02 forbids (§4), the registered push is rejected as a
    validation WHEN IT RUNS, makes no Jira call, and is recorded as a failed
    push with no comment and no retry — which is what `_push`'s deferred
    branch already does with any failure."""
    if _no_issue_to_write(item, STEP_STATUS):
        return
    _push(item, _status_call(item, canonical_status, None, STEP_STATUS),
          step=STEP_STATUS,
          deferred_failure_reason=f'the derived push of status "{canonical_status}" to Jira failed')


def push_comment(item: WorkItem, author: str, body: str, *, completion_key: Optional[str] = None) -> dict:
    """The comment path's Jira write (REQ-09, "One comment path, in every
    mode"). Posts `[<author>] <body>` so the author survives the round
    trip: `_handle_comment_event` strips that prefix and records it as the
    author when the webhook's comment is from the integration account.

    `completion_key` is `append_comment`'s own `source_message_id` — in
    Jira mode nothing is recorded, so that id becomes the completion
    record's key instead of the comment row's dedupe key."""
    step = STEP_COMMENT
    if _no_issue_to_write(item, step):
        return {'posted': True, 'workItemId': str(item.id), 'id': None}
    if is_step_complete(completion_key, item.id, step):
        return {'posted': True, 'workItemId': str(item.id), 'id': None}

    def call():
        if not _still_jira_mode(item.project):
            return
        jira_client.post_comment(item.external_key, f'[{author}] {body}')
        mark_step_complete(completion_key, item.id, step)

    _push(item, call, step=step, deferred_failure_reason='posting a comment to Jira failed')
    return {'posted': True, 'workItemId': str(item.id), 'id': None}


def push_assignment(item: WorkItem, agent_id: str, *, completion_key: Optional[str] = None) -> dict:
    """An assignment → `set_agent_field`. `webhook_consumer.py` reads the
    Agent-field change back as a validated assignment."""
    step = STEP_ASSIGNMENT
    if _no_issue_to_write(item, step):
        return {'posted': True, 'workItemId': str(item.id)}
    if is_step_complete(completion_key, item.id, step):
        return {'posted': True, 'workItemId': str(item.id)}

    def call():
        if not _still_jira_mode(item.project):
            return
        jira_client.set_agent_field(item.external_key, agent_id)
        mark_step_complete(completion_key, item.id, step)

    return _push(item, call, step=step, deferred_failure_reason='setting the Agent field in Jira failed')


def push_link(blocker: WorkItem, dependent: WorkItem, *, completion_key: Optional[str] = None) -> dict:
    """A link creation → `create_issue_link` with the Blocks link type,
    unless `get_issue_links` already shows it (Jira does not dedupe
    identical links). The result names the DEPENDENT, `create_link`'s
    `to_work_item_id` — the item whose history records a link."""
    step = STEP_LINK
    if _no_issue_to_write(blocker, step) or _no_issue_to_write(dependent, step):
        return {'posted': True, 'workItemId': str(dependent.id)}
    if is_step_complete(completion_key, dependent.id, step):
        return {'posted': True, 'workItemId': str(dependent.id)}

    def call():
        if not _still_jira_mode(dependent.project):
            return
        links = jira_client.get_issue_links(dependent.external_key)
        if blocker.external_key not in (links.get('isBlockedBy') or []):
            jira_client.create_issue_link(blocker.external_key, dependent.external_key)
        mark_step_complete(completion_key, dependent.id, step)

    return _push(dependent, call, step=step, deferred_failure_reason='creating a Jira issue link failed')


def pending_comment_steps(message_id: str, work_item_id) -> list[str]:
    """The names of this `messageId`'s comment steps that were registered
    and are not yet complete, oldest first. `_handle_story_created`'s early
    return on a redelivery runs exactly these (REQ-09, "Canonical events
    with a Jira side effect"): a first delivery for a Story whose key
    already has a row registers none, so it posts no story-intake comment
    at all."""
    prefix = f'{message_id}:'
    rows = (
        JiraWriteCompletion.objects
        .filter(work_item_id=work_item_id, step=STEP_COMMENT,
                completion_key__startswith=prefix, completed_at__isnull=True)
        .order_by('registered_at')
    )
    return [row.completion_key[len(prefix):] for row in rows]


# ---------------------------------------------------------------------------
# Canonical events with a Jira side effect (REQ-09)
# ---------------------------------------------------------------------------

WRITER_GROUP = 'jira-writer'

# The Agent field value story intake assigns. The `story_intake` side effect
# does not carry it (`webhook_consumer.py`'s `_handle_story_created` sets
# `assigneeAgentId` itself), so the writer supplies it, as V1's
# `handleStoryCreated` did.
STORY_INTAKE_AGENT = 'refinement-agent'


def push_release_candidate_fields(item: WorkItem, *, candidate_sha: str, build_identifier: Optional[str] = None,
                                   preview_url: Optional[str] = None) -> dict:
    """The candidate fields' push (release-mode-parity.md REQ-13, "The
    report"): the SHA, build identifier and preview URL sent to the Release
    ticket in ONE edit (`jira_client.set_fields`), so they reach `core`
    together on one webhook, which records them and runs the recording step.
    Nothing is recorded here.

    Called by `store.report_release_candidate` with no transaction open, so
    the edit is made now and a failure propagates to the release-candidate
    job, which fails visibly. A field whose id is not configured is left out
    of the edit, as v5.2's per-field writes left it out — but when none of the
    three ids is configured the push would write nothing, and that fails the
    same way a failed edit does (one webhook failure recorded, the error
    propagating; nothing is recorded and no native-build comment follows)."""
    import os

    step = STEP_RELEASE_FIELDS
    if _no_issue_to_write(item, step):
        return {'posted': True, 'workItemId': str(item.id), 'skipped': 'no-external-key'}

    values = {'candidateSha': candidate_sha, 'buildIdentifier': build_identifier, 'previewUrl': preview_url}
    fields = {}
    for env_name, key in (
        ('JIRA_CANDIDATE_SHA_FIELD_ID', 'candidateSha'),
        ('JIRA_BUILD_IDENTIFIER_FIELD_ID', 'buildIdentifier'),
        ('JIRA_PREVIEW_URL_FIELD_ID', 'previewUrl'),
    ):
        field_id = os.environ.get(env_name)
        if field_id:
            fields[field_id] = values[key] or None

    def call():
        if not _still_jira_mode(item.project):
            return
        if not fields:
            logger.error('[jira-writer] no candidate field id is configured — nothing to write to %s',
                          item.external_key)
            record_webhook_failure(item, 'no release-candidate field id is configured — the candidate '
                                         'was not written to Jira', {'step': step})
            raise RuntimeError('no release-candidate field id is configured — the candidate was not '
                               'written to Jira')
        jira_client.set_fields(item.external_key, fields)

    return _push(item, call, step=step, deferred_failure_reason='pushing the release candidate to Jira failed')


def _handle_release_candidate_recorded(item: WorkItem, data: dict, completion_key: Optional[str]) -> None:
    """`transition_issue` to the Jira status the project's map writes for
    `in-review` — and nothing else (release-mode-parity.md REQ-13, "The
    writer"). The three candidate fields are already in Jira: in Jira mode
    the report pushed them first (`push_release_candidate_fields`) and this
    event was published from their webhook; in local mode this handler makes
    no Jira call at all (`handle_event_envelope`'s mode check). The note is
    the recording step's own call to the comment path, so it reaches Jira
    once and is not this handler's."""
    if is_step_complete(completion_key, item.id, STEP_STATUS):
        return
    jira_status = canonical_to_jira_status(item.project, 'in-review')
    if jira_status is None:
        record_webhook_failure(
            item, 'this project\'s Jira status map writes no Jira status for "in-review"',
            {'step': STEP_STATUS, 'event': 'work_item.release_candidate_recorded'},
        )
        mark_step_complete(completion_key, item.id, STEP_STATUS)
        return
    _transition_or_record_outcome(item, jira_status, completion_key=completion_key, step=STEP_STATUS)


def is_step_pending(completion_key: Optional[str], work_item_id, step: str) -> bool:
    """A step this key REGISTERED and has not completed — the redelivery
    rule for a step registered ahead of its work (v5.2 REQ-09)."""
    if not completion_key:
        return False
    return JiraWriteCompletion.objects.filter(
        completion_key=completion_key, work_item_id=work_item_id, step=step, completed_at__isnull=True,
    ).exists()


def push_release_in_progress(item: WorkItem, *, completion_key: Optional[str]) -> Optional[dict]:
    """A Jira-mode release request's In Progress push (release-mode-parity.md
    REQ-09): the Jira status the project's map writes for `in-progress`, as
    a step keyed `<messageId>:release-in-progress` that
    `_handle_release_requested` registers in the block that publishes
    `requested`. `core` records `in-progress` from the ticket's webhook and
    publishes nothing for it (v5.2 REQ-08's guard).

    Made only when the step is registered and not complete, so a push that
    failed is made on redelivery, one that succeeded is not repeated, and a
    refused request — which registers none — is never pushed. A Release
    `core` no longer holds at `proposed` (a creation webhook replayed after
    its candidate) has its step recorded complete with no push, so the
    ticket never moves back. It does not go through
    `store.transition_status`, so the release gate does not run twice.

    Called with no transaction open, so the push is made now and a failure
    propagates: the webhook message is redelivered."""
    if not is_step_pending(completion_key, item.id, STEP_STATUS):
        return None
    current = WorkItem.objects.filter(id=item.id).first()
    if current is None or current.status != 'proposed':
        mark_step_complete(completion_key, item.id, STEP_STATUS)
        return None
    return push_status(current, 'in-progress', completion_key=completion_key)


def _handle_story_intake_side_effect(item: WorkItem, detail: dict, completion_key: Optional[str]) -> None:
    """The three branches the side effect carries — reblock, accepted, and
    accepted with missing fields — as the Agent field and the Blocked flag.
    Each branch's COMMENT is not part of the event: the webhook consumer
    appends it through the comment path after its own transaction block
    exits (REQ-09), so this handler never posts one.

    The Agent field is set in every branch. It is the same value in all
    three (`refinement-agent`) and setting it is idempotent, so the reblock
    branch — where V1 re-blocked without touching it — costs one extra PUT
    and cannot diverge from the other two."""
    if not is_step_complete(completion_key, item.id, STEP_AGENT_FIELD):
        jira_client.set_agent_field(item.external_key, STORY_INTAKE_AGENT)
        mark_step_complete(completion_key, item.id, STEP_AGENT_FIELD)

    blocked = not detail.get('ok')
    if blocked and not is_step_complete(completion_key, item.id, STEP_BLOCKED_FLAG):
        jira_client.set_blocked_field(item.external_key, True)
        mark_step_complete(completion_key, item.id, STEP_BLOCKED_FLAG)


def handle_event_envelope(envelope: dict[str, Any]) -> None:
    """One entry of a project's `aigang:workitems:{project}:events` stream.

    `work_item.status_changed` makes no write: in Jira mode `core` records a
    status only from Jira's webhook, and a derived write is pushed before it
    is recorded, so an event describing a recorded status has nothing left
    to do. Any event kind with no Jira side effect is acknowledged with no
    write."""
    payload = envelope.get('payload') or {}
    event_type = payload.get('eventType')
    data = payload.get('data') or {}
    project = envelope.get('project')
    completion_key = envelope.get('messageId')

    if event_type not in ('work_item.release_candidate_recorded', 'work_item.jira_side_effect'):
        return

    if not project or write_gate.mode_of(project) != project_config.JIRA:
        return  # a local-mode project's inputs make no Jira call.

    work_item_id = payload.get('workItemId') or data.get('id') or data.get('workItemId')
    if not work_item_id:
        return
    item = WorkItem.objects.filter(id=work_item_id).first()
    if item is None:
        return
    if _no_issue_to_write(item, event_type):
        return

    if event_type == 'work_item.release_candidate_recorded':
        _handle_release_candidate_recorded(item, data, completion_key)
        return

    if data.get('kind') == 'story_intake':
        _handle_story_intake_side_effect(item, data.get('detail') or {}, completion_key)


def create_writer_consumer(redis_factory, project: str, *, consumer_name: str | None = None):
    """The writer's own consumer group over a project's event stream,
    created at the stream's END rather than `streams.ensure_group`'s default
    of its start, so nothing published before the group existed is written
    (v5.1 audit SR-5-02). `create_consumer` calls `ensure_group`, which is
    idempotent, so creating the group here first is what decides where it
    starts; a group that already exists keeps its position."""
    import os

    from .streams import create_consumer
    from .stream_topology import event_stream_name

    # The records outlive the longest replay window and no longer (30 days,
    # `streams.trim_dead_letters`'s own default), trimmed once per process
    # start rather than per event: the table is bookkeeping, and a scan of
    # it on every event would cost more than the rows it removes.
    prune_completion_records()

    stream = event_stream_name(project)
    client = redis_factory()
    try:
        client.xgroup_create(stream, WRITER_GROUP, id='$', mkstream=True)
    except Exception as err:  # noqa: BLE001 - BUSYGROUP means the group already exists, with its own position
        if 'BUSYGROUP' not in str(err):
            raise

    def handler(envelope: dict[str, Any]) -> None:
        handle_event_envelope(envelope)

    return create_consumer(
        redis_factory,
        stream=stream,
        group=WRITER_GROUP,
        consumer_name=consumer_name or os.uname().nodename,
        handler=handler,
    )


# ---------------------------------------------------------------------------
# A Jira-mode decomposition (canonical-delivery-state.md REQ-11)
# ---------------------------------------------------------------------------
#
# `materialize.materialize_decomposition` routes the command here on `push`
# (REQ-09, "The routing layer"), having already run every validation it runs
# in local mode apart from the gate — the assignment check, the no-progress
# rule over the proposal graph and each proposal's artifact resolution —
# with nothing written, so a rejected decomposition creates nothing in Jira.
#
# Nothing canonical is recorded here either. Each Jira Sub-task reaches
# `core` through its own `jira:issue_created` webhook, which REQ-11's mirror
# materializes with the proposal id this module's record holds for its key,
# so a Jira-mode subtask ends up with the SAME canonical id local mode would
# have given it.

STEP_DECOMPOSITION = 'decomposition'


def jira_project_key_of(project: str) -> Optional[str]:
    """The Jira project key a Jira-mode project's issues are created in
    (REQ-11: "with the project key from `ProjectConfig.jira_project_key`").

    Read off the row directly rather than through
    `project_config.get_mode`, which also returns the mode: the key is not
    the mode, and `REQ-09/mode-readers` makes "no `get_mode` call outside
    the mode layer" a property of the built Source. `write_gate.mode_of`
    stays this module's one mode read."""
    row = ProjectConfig.objects.filter(project=project).only('jira_project_key').first()
    return (row.jira_project_key or None) if row is not None else None


def proposal_record(proposal_id) -> Optional[JiraDecompositionProposal]:
    return JiraDecompositionProposal.objects.filter(proposal_id=proposal_id).first()


def proposal_record_for_key(jira_key: str) -> Optional[JiraDecompositionProposal]:
    """The record row whose Jira key is `jira_key`, which is how REQ-11's
    mirror finds a mirrored Sub-task's proposal id, specification link and
    artifact links."""
    if not jira_key:
        return None
    return JiraDecompositionProposal.objects.filter(jira_key=jira_key).first()


def keyed_proposal_ids(subtasks: list[dict]) -> set:
    """The ids among `subtasks` the record already holds a Jira key for — a
    redelivery's already-created proposals, which count as resolved
    blockers without being created again."""
    ids = [s.get('id') for s in (subtasks or []) if s.get('id')]
    if not ids:
        return set()
    rows = JiraDecompositionProposal.objects.filter(
        proposal_id__in=ids, jira_key__isnull=False,
    ).values_list('proposal_id', flat=True)
    keyed = {str(proposal_id) for proposal_id in rows}
    return {s['id'] for s in subtasks if s.get('id') and str(s['id']) in keyed}


def inward_record_pairs(jira_key: str) -> list[dict]:
    """The Blocks pairs the record holds with `jira_key` as the DEPENDENT —
    a subtask's inward blockers as REQ-11 defines them for a subtask the
    record names ("every Blocks pair the record holds with its key as the
    dependent")."""
    row = proposal_record_for_key(jira_key)
    return list(row.blocks_pairs or []) if row is not None else []


def record_pairs_naming_key(jira_key: str) -> list[dict]:
    """Every Blocks pair the record holds in which `jira_key` is EITHER end
    — what the mirror adds as canonical `blocks` links when it creates the
    subtask (Shovel Ready Pass 6, decision 2.1)."""
    if not jira_key:
        return []
    pairs = list(inward_record_pairs(jira_key))
    for row in JiraDecompositionProposal.objects.filter(
            blocks_pairs__contains=[{'blockerKey': jira_key}]):
        pairs.extend(pair for pair in (row.blocks_pairs or []) if pair.get('blockerKey') == jira_key)
    return pairs


def record_holds_pair(blocker_key: str, dependent_key: str) -> bool:
    """Whether the record holds this Blocks pair. A reconciled link the
    record holds whose other end has no canonical row yet is SKIPPED with
    no webhook failure, because the mirror will add it when that end is
    created (REQ-11; Shovel Ready Pass 7, SR-7-11)."""
    if not blocker_key or not dependent_key:
        return False
    return any(pair.get('blockerKey') == blocker_key for pair in inward_record_pairs(dependent_key))


def dependent_proposal_ids_for_blocker(blocker_work_item_id) -> list:
    """The canonical ids of the dependents the record holds for this
    blocker. `store._unblock_dependents` adds these to the dependents it
    finds through canonical `blocks` links, "in every mode", reading no
    mode (REQ-11; §4's "Jira-mode unblocking from the decomposition
    record" row): a Jira link may land after its subtask, so a canonical
    link may not exist yet when the blocker reaches `done`.

    Matched on the blocker's PROPOSAL id, not its Jira key, because the
    proposal id is a mirrored subtask's canonical id (REQ-11) and is known
    before any Jira call is made — so this needs no `external_key` and
    works for a project that has since been returned to local mode."""
    rows = JiraDecompositionProposal.objects.filter(
        blocks_pairs__contains=[{'blockerProposalId': str(blocker_work_item_id)}],
    ).values_list('proposal_id', flat=True)
    return list(rows)


def _record_row_for(project: str, parent: WorkItem, proposal: dict) -> JiraDecompositionProposal:
    """The record row for one proposal, created if this is its first
    delivery. Carries the proposal's specification and artifact links so
    whichever of the mirror and this record lands second can attach them to
    the canonical subtask (REQ-11)."""
    spec_link = proposal.get('specificationLink') or None
    artifact_links = [str(artifact_id) for artifact_id in (proposal.get('artifactLinks') or [])]
    row, created = JiraDecompositionProposal.objects.get_or_create(
        proposal_id=proposal['id'],
        defaults={
            'project': project,
            'parent_work_item_id': parent.id,
            'specification_link': ({'artifactId': str(spec_link['artifactId']),
                                     'requirementId': spec_link['requirementId']} if spec_link else None),
            'artifact_links': artifact_links,
        },
    )
    return row


def _record_blocks_pair(dependent_proposal_id, blocker_proposal_id, blocker_key: str,
                         dependent_key: str) -> None:
    """Recorded immediately after the Jira link is created, on the
    DEPENDENT's row, so a redelivery skips it and the mirror can read a
    subtask's inward blockers off one row."""
    row = JiraDecompositionProposal.objects.filter(proposal_id=dependent_proposal_id).first()
    if row is None:
        return
    pairs = list(row.blocks_pairs or [])
    if any(pair.get('blockerKey') == blocker_key for pair in pairs):
        return
    pairs.append({
        'blockerProposalId': str(blocker_proposal_id),
        'dependentProposalId': str(dependent_proposal_id),
        'blockerKey': blocker_key,
        'dependentKey': dependent_key,
    })
    row.blocks_pairs = pairs
    row.save(update_fields=['blocks_pairs'])


def attach_record_references(row: JiraDecompositionProposal) -> None:
    """Attach a proposal's specification and artifact links to its canonical
    subtask, if that subtask exists yet.

    REQ-11: "Whichever of the mirror and the writer's key record lands
    second attaches the proposal's specification and artifact links to the
    canonical subtask; each side commits its own write before it looks for
    the other's, and both attaches are idempotent". This is that one
    attach, called from both sides: here, right after the writer commits a
    proposal's Jira key (the normal order is that no canonical row exists
    yet, so this does nothing), and from the mirror right after it commits
    the subtask (which is the side that normally does the work). §4 records
    the window this covers from this side: a Sub-task webhook that beats
    the writer's key record is mirrored with a fresh id and no record, and
    its references would otherwise never be attached at all.

    Idempotent on both paths: `store._record_specification_link` returns
    the existing row unchanged for the same pair, and `_add_artifact_link`
    dedupes on (work item, artifact)."""
    from . import store  # lazy: store imports this module at import time.

    if not row.jira_key:
        return
    item = WorkItem.objects.filter(external_key=row.jira_key).first()
    if item is None:
        return

    spec_link = row.specification_link or None
    if spec_link:
        store.record_specification_link(item.id, spec_link['artifactId'], spec_link['requirementId'],
                                        actor=f'jira-writer:{row.jira_key}')
    for artifact_id in (row.artifact_links or []):
        store.add_artifact_link(item.id, artifact_id, actor=f'jira-writer:{row.jira_key}')


def execute_decomposition(project: str, parent: WorkItem, plan: dict, *,
                           completion_key: Optional[str] = None) -> dict:
    """REQ-11's Jira-mode decomposition: a Sub-task per proposal under the
    parent's issue with its Agent field set, a Blocks link per edge of the
    proposal graph, and the Jira status the project's map writes for
    `ready` on the root subtasks (Shovel Ready with
    `DEFAULT_JIRA_STATUS_MAP`).

    `plan` is `materialize.plan_decomposition`'s result: `order` (every
    proposal, blockers first), `pairs` (the Blocks edges) and `rootIds`.

    **Each key is recorded the moment its issue exists, and each pair the
    moment its link does**, so a redelivery skips every recorded proposal
    and link. Called with no transaction open — `materialize`'s pushing
    path opens none and the command consumer holds none — so each of those
    records commits before the next create, which is what makes a failure
    part way through resumable (§4 records the crash window between a Jira
    create and its record as a known limitation)."""
    if _no_issue_to_write(parent, STEP_DECOMPOSITION):
        return {'posted': True, 'workItemId': str(parent.id), 'skipped': 'parent-has-no-external-key'}

    project_key = jira_project_key_of(project)
    if not project_key:
        logger.error('[jira-writer] project %s is in Jira mode with no jira_project_key — '
                      'cannot create a Sub-task', project)
        record_webhook_failure(
            parent,
            'project is in Jira mode with no jira_project_key recorded — no Sub-task could be created',
            {'step': STEP_DECOMPOSITION},
        )
        return {'posted': True, 'workItemId': str(parent.id), 'skipped': 'no-jira-project-key'}

    id_to_jira_key: dict[str, str] = {}
    for proposal in plan['order']:
        row = _record_row_for(project, parent, proposal)
        if not row.jira_key:
            row.jira_key = jira_client.create_subtask_for_proposal(
                parent.external_key, project_key,
                summary=proposal.get('displayName') or str(proposal['id']),
                description=proposal.get('description'),
                agent_field_value=proposal.get('agent'),
            )
            row.save(update_fields=['jira_key'])
        # "each side commits its own write before it looks for the other's"
        # — the key is committed above, so this is where the writer attaches
        # the proposal's references if the mirror got there first (§4's
        # window; normally there is no canonical row yet and this is a
        # single SELECT).
        attach_record_references(row)
        id_to_jira_key[str(proposal['id'])] = row.jira_key

    for blocker_id, dependent_id in plan['pairs']:
        blocker_key = id_to_jira_key.get(str(blocker_id))
        dependent_key = id_to_jira_key.get(str(dependent_id))
        if not blocker_key or not dependent_key:
            continue  # unreachable: the plan resolves every blocker to a proposal in this command.
        if record_holds_pair(blocker_key, dependent_key):
            continue
        links = jira_client.get_issue_links(dependent_key)
        if blocker_key not in (links.get('isBlockedBy') or []):
            # Jira does not dedupe identical links, so the check is the
            # client's documented precondition, not an optimization.
            jira_client.create_issue_link(blocker_key, dependent_key)
        _record_blocks_pair(dependent_id, blocker_id, blocker_key, dependent_key)

    ready_status = canonical_to_jira_status(project, 'ready')
    for proposal_id in plan['rootIds']:
        issue_key = id_to_jira_key.get(str(proposal_id))
        if not issue_key or is_step_complete(completion_key, proposal_id, STEP_STATUS):
            continue
        if ready_status is None:
            # V2 REQ-02 forbids this configuration (§4); should it exist
            # anyway, the push is recorded as one webhook failure and not
            # retried, and the subtask keeps the status Jira gave it.
            record_webhook_failure(
                parent,
                'this project\'s Jira status map writes no Jira status for "ready" — '
                f'root Sub-task {issue_key} was left as Jira created it',
                {'step': STEP_STATUS, 'proposalId': str(proposal_id), 'jiraKey': issue_key},
            )
            mark_step_complete(completion_key, proposal_id, STEP_STATUS)
            continue
        outcome = transition_outcome(project, issue_key, ready_status)
        if not outcome['transitioned'] and not (outcome['alreadyMapped'] and not outcome['flagged']):
            record_webhook_failure(
                parent,
                f'Jira issue {issue_key} offers no transition to "{ready_status}"',
                {'step': STEP_STATUS, 'proposalId': str(proposal_id),
                 'targetJiraStatus': ready_status, 'currentJiraStatus': outcome['currentJiraStatus'],
                 'blocked': outcome['flagged']},
            )
        mark_step_complete(completion_key, proposal_id, STEP_STATUS)

    return {'posted': True, 'workItemId': str(parent.id), 'idToJiraKey': id_to_jira_key}


def move_writer_group_to_stream_end(project: str, *, client=None) -> None:
    """Move this writer's consumer group on a project's event stream to the
    stream's END — what REQ-10's `connect_jira` does at the switch, "so
    nothing published while the project was local is written to Jira".

    A project that was local has been recording status changes, comments
    and links into its event stream all along; after the switch the writer
    starts consuming that stream, and without this it would begin at
    whatever position the group already held (or at the stream's start for
    a group created fresh) and replay a project's entire local history into
    Jira as new writes.

    Idempotent, and safe to call for a stream that does not exist yet
    (`mkstream`)."""
    from .redis_client import get_client
    from .stream_topology import event_stream_name

    stream = event_stream_name(project)
    client = client or get_client()
    try:
        client.xgroup_create(stream, WRITER_GROUP, id='$', mkstream=True)
    except Exception as err:  # noqa: BLE001 - BUSYGROUP means the group exists and has to be MOVED
        if 'BUSYGROUP' not in str(err):
            raise
        client.xgroup_setid(stream, WRITER_GROUP, id='$')
