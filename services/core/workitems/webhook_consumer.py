"""
Durable, retried webhook ingestion; Jira-originated events are validated
before being applied; and all platform-specific interpretation lives in
Django, with every ingested event republished.

**Amended 2026-09-09 to close a durability/coverage gap.** This module
originally only applied a `changelog` entry with `field == 'status'` and
silently ignored everything else (comments, issue links, issue creation,
Release events) — a known, tracked gap. It now interprets the full webhook
payload:

  - `jira:issue_created` for a Story: creates the canonical work item
    (the five-field gate decides whether it starts `ready`
    (dispatch-eligible) or stuck in `proposed`), matching
    handlers.js's `handleStoryCreated`.
  - `jira:issue_created` for a Release: materializes (or, on redelivery,
    reuses) a canonical `release` work item and its
    `work_item_release_detail` row from the ticket's five fields
    (`jira_interpret.parse_release_fields` — BUGFIXES.md BF-01), THEN
    publishes `work_item.jira_release_event` (kind `requested`) through
    `store.publish_release_event` only when this webhook materializes the
    Release (REQ-10), so a redelivery publishes nothing again — see the
    module docstring section below on the Release scope carve-out, which
    this does not reopen.
  - `comment_created`/`comment_updated`: projected into the canonical
    comment thread via `store.append_comment` (previously
    silently discarded).
  - `jira:issue_updated` changelog, per item:
      - for a Release ticket already materialized into a `release` work
        item: `work_item_release_detail` is re-synced from the webhook's
        current `issue.fields` snapshot first (`_sync_release_detail`,
        mirroring `_sync_story_detail`) — this is how the release-candidate
        Jenkins job's report to `core`'s release-candidate endpoint, which
        in Jira mode pushes Candidate SHA/Build Identifier/Preview URL onto
        the ticket in one edit (REQ-13), reaches the canonical columns,
        regardless of which changelog field the webhook names.
      - for a Release ticket with NO canonical `release` work item yet
        (e.g. one created before BF-01 shipped, whose `jira:issue_created`
        webhook came and went unmaterialized): materialized first, via the
        same idempotent-on-`external_key` `_materialize_release` helper
        `jira:issue_created` uses, before any of the field-specific
        handling below runs — BUGFIXES.md BF-01 Pass 1 audit row 2, REQ-01
        "create or update webhook". This does not publish a
        `work_item.jira_release_event`; only the `jira:issue_created`
        path does that.
      - `field == 'status'`: the existing validated transition
        path (`_apply_validated_status_change`), now also branching to a
        `work_item.jira_release_event` for a Release ticket's `Done`
        transition (production-promote gate).
      - the configured `JIRA_BLOCKED_FIELD_ID` custom field going from
        truthy to falsy ("Blocked cleared"): matches handlers.js's
        `handleBlockedCleared` — see `_handle_blocked_field_change`.
      - a Release's move to Done or to Abandoned (a status change, through
        the project's map, to `done` or `cancelled`): the status and its
        `work_item.jira_release_event` (kind `done` or `abandoned`) in one
        transaction, only when the webhook changes the canonical status
        (release-mode-parity.md REQ-10, REQ-14).
      - anything else (issue links, arbitrary custom fields): durably
        recorded and republished as a generic `work_item.jira_event_received`
        event rather than dropped — a consumer with no use for a
        given event today may ignore it, but Django must not decide on
        ingestion that an event is irrelevant and drop it.

**Scope carve-out, narrowed by BF-01 (`v2.1/BUGFIXES.md`), still otherwise
in force (see final report for the full reasoning):**
Release-ticket BUSINESS LOGIC (the beta-queue-clean check ahead of cutting
a release candidate, and the Jenkins job triggers themselves) is NOT
reimplemented here. Django/`core` holds the running platform's only Jira
client (`workitems/jira_client.py`, V5.1 REQ-01); no running consumer calls
it until v5.2's outbound writer, besides `ensure_jira_webhook.py`'s webhook
registration (BF-02), and Jenkins' own Jira writes remain until v5.2
(`jira-integration-relocation.md` REQ-06, REQ-08). From v5.1 nothing acts on
a Jira-mode release event: ScrumMaster's write and read call sites into
Jira, including the Release branch that used to run this beta-queue-clean
check and trigger the Jenkins jobs, are deleted outright (REQ-04, REQ-05),
and a `work_item.jira_release_event` for a project in Jira mode, or one that
carries no `workItemId`, is logged by ScrumMaster at error level as
unresolved and triggers no Jenkins job (REQ-04). What BF-01 adds is narrower
and purely representational: recording the ticket's own fields as a
canonical `release` work item and `work_item_release_detail` row, the SAME
table and columns REQ-01 already gives local-mode releases, so a Jira-mode
release is observable through the same internal read API
(`GET /work-items/<id>?full=true`). It does not decide candidate-cut
eligibility, does not trigger Jenkins, and does not change
`work_item_release_detail`'s shape.

**Corrected 2026-10-04 (v5.1 BUGFIXES.md BF-06; v5.2 Canonical Delivery
State REQ-06, REQ-08, REQ-09).** The paragraph above describes `ba3f68b`,
before this stage, and three of its premises no longer hold. `core`'s
outbound writer (`workitems/jira_writer.py`) is now a running caller of
`jira_client.py` — the client does not wait for an "outbound writer" that
has since been built, and `views.py` and this module call it directly too
(`views.py`'s `jira_client.get_issue`; this module's
`get_blocks_link_type_id` and `get_issue`, for Blocks link-type and
issue-key lookups; the Blocked-flag write goes through `jira_writer`'s
`set_blocked_field`). The webhook registration (`ensure_jira_webhook.py`)
still has its own HTTP helper, `_jira_request`, and does not use the
client. Jenkins' own Jira writes are not
merely "remaining until v5.2": REQ-06 retires every one of them, and the
candidate-cut writes this module's Release handling used to route to
Jenkins instead happen as Django-side effects of REQ-09's writer. And a
`work_item.jira_release_event` for a Jira-mode project no longer logs as
unresolved and triggers no job: REQ-08 removes `dispatchConsumer.js`'s
Jira-mode early return, so ScrumMaster triggers the candidate,
production-promote and preview-teardown jobs for a Jira-mode release the
same way it does for a local-mode one. What still holds is the
representational point this paragraph was making: a Release ticket's own
candidate-cut eligibility and promotion logic is Django's to decide, not
reimplemented by republishing a generic event from this consumer.

Design decision (carried over unchanged from before this amendment):
The durability requirement itself is satisfied by
`workitems/views.py`'s `jira_webhook` view, which now durably enqueues
every inbound Jira webhook onto `aigang:webhooks:{project}` — moved here
from `services/scrummaster/src/server.js` since Django is now AI Gang's
sole external-facing surface. This module remains the
consumer half: the `core` consumer group on that same stream.
"""

from __future__ import annotations

import os
import re
import uuid
from typing import Any, Callable, Optional

from django.db import transaction
from django.utils import timezone

from . import (
    assignment, jira_client, jira_interpret, jira_writer, project_config, registry, status_vocabulary,
    store, write_gate,
)
from .models import ProjectStatusConfig, WebhookFailure, WorkItem, WorkItemReleaseDetail, WorkItemStoryDetail
from .streams import create_consumer

WEBHOOK_GROUP = 'core'

# Test-only knob (mirrors relay.py's ROW_DELAY_MS) letting a kill-mid-run
# test reliably catch this consumer partway through a batch instead of
# racing a batch that completes in a few milliseconds.
_ROW_DELAY_ENV = 'WEBHOOK_CONSUMER_ROW_DELAY_MS'


def record_failure(project: str, work_item_id, external_key: Optional[str], reason: str, payload: Optional[dict]) -> None:
    WebhookFailure.objects.create(
        project=project, work_item_id=work_item_id, external_key=external_key, reason=reason,
        payload=payload or {}, occurred_at=timezone.now(),
    )


def default_resolve_work_item_id(issue_key: str):
    row = WorkItem.objects.filter(external_key=issue_key).first()
    return row.id if row else None


# Each canonical status must map to a
# configured Jira status through validated project configuration, not a
# hardcoded or display-label-dependent mapping. This is the fallback for a
# project that hasn't declared its own mapping via
# project_config.declare_custom_status — not itself the validated
# configuration that rule requires.
DEFAULT_JIRA_STATUS_MAP = {
    'Shovel Ready': 'ready',
    'In Progress': 'in-progress',
    'In Review': 'in-review',
    'Done': 'done',
    'Backlog': 'proposed',
    # release-mode-parity.md REQ-14: abandonment, both ways and for every
    # issue type — inbound a move to Abandoned records `cancelled`, outbound
    # the writer writes `cancelled` as Abandoned (no longer the Blocked flag).
    'Abandoned': 'cancelled',
}


def jira_status_to_canonical(project: str, jira_status_name: str) -> Optional[str]:
    """**The one inbound status map, chosen PER PROJECT**
    (canonical-delivery-state.md REQ-09, "A status write to any other
    status"; Shovel Ready Pass 8, answer 1.1), in the order
    `canonical-work-model.md` REQ-11's resolution records: a project with
    `ProjectStatusConfig` rows is mapped by its rows ALONE, and
    `DEFAULT_JIRA_STATUS_MAP` maps a project with none.

    Returns None when the project's map does not name `jira_status_name` —
    including a Jira status only `DEFAULT_JIRA_STATUS_MAP` names, for a
    project that has rows. There is then no canonical status to record, and
    the caller records none (`_apply_validated_status_change`).

    `jira_writer.canonical_to_jira_status` is this function's inverse, and
    the writer calls this one for the forward direction rather than keeping
    a second copy: the two have to agree on which map a project uses, and
    the only way to guarantee that is for there to be one map.

    **Two behaviours changed at v5.2**, both because this function was
    consulted as though it were per-project when it was not:

      - it no longer falls back to `DEFAULT_JIRA_STATUS_MAP` for a project
        that has declared rows. Up to v5.1 a project that declared one row
        still inherited the whole default map, so a webhook moving an issue
        to a default-map name the project had deliberately not declared
        changed the canonical status anyway;
      - it no longer falls through to the raw `jira_status_name`. That
        passthrough made an unmapped status indistinguishable from a
        declared one until `status_vocabulary.validate_status` rejected it
        one layer later, which is a rejection about the canonical vocabulary
        reported as though the transition had been attempted.

    Rows are read in id order so a tie — two rows naming the same Jira
    status — resolves to the lowest-id row, the same rule the inverse
    direction follows. `project_config.get_custom_statuses` is not used
    here because it returns no id and imposes no ordering, and the tie rule
    needs both."""
    rows = list(
        ProjectStatusConfig.objects.filter(project=project)
        .order_by('id').values('status', 'jira_status_name')
    )
    if rows:
        for row in rows:
            if row['jira_status_name'] == jira_status_name:
                return row['status']
        return None
    return DEFAULT_JIRA_STATUS_MAP.get(jira_status_name)


# canonical-delivery-state.md REQ-09, "Canonical events with a Jira side
# effect": story intake's comments are not part of the `story_intake` event.
# This module appends them through the comment path AFTER its handler's
# `transaction.atomic()` block exits, because in Jira mode that path posts to
# Jira in-process and a Jira call must not run inside an open transaction.
# The texts are V1's own, from `e39e9ab`'s `handlers.js` — `:142` (the
# acknowledgement), `:153-156` (missing fields) and `:245-248` (the reblock).
# Each is a step keyed `<messageId>:<name>` on the webhook envelope.
STORY_INTAKE_COMMENT_AUTHOR = 'system'

# The author of the candidate note the recording step posts when a Release's
# webhook records a new candidate (release-mode-parity.md REQ-13) — the
# webhook does not say whether the job or a person set the SHA.
RELEASE_AUTHOR = 'system'
STORY_INTAKE_ACKNOWLEDGEMENT = 'Ticket received. Assigned to Refinement Agent for decomposition.'


def _missing_fields_list(missing: list[str]) -> str:
    return '\n'.join(f'  - {label}' for label in missing)


def _missing_fields_comment(missing: list[str]) -> str:
    return (
        'Story is missing required fields and cannot be refined until they are filled in:\n\n'
        f'{_missing_fields_list(missing)}\n\n'
        'Please complete these fields and move the ticket back to Backlog to retry.'
    )


def _reblock_comment(missing: list[str]) -> str:
    return (
        'Story is still missing required fields and cannot be refined until they are filled in:\n\n'
        f'{_missing_fields_list(missing)}\n\n'
        'Please complete these fields and clear the Blocked field again to retry.'
    )


def _register_comment_steps(message_id: Optional[str], work_item_id, names) -> None:
    """Registered inside the handler's own atomic block, ahead of the work
    — the one case REQ-09's "Redelivery" allows that, because
    `_handle_story_created`'s early return on a redelivery has no other way
    to know which comment steps this `messageId` owes. A registered step is
    not complete and is not skipped."""
    if not message_id:
        return
    for name in names:
        jira_writer.register_step(f'{message_id}:{name}', work_item_id, jira_writer.STEP_COMMENT)


def _post_comment_steps(work_item_id, message_id: Optional[str], steps) -> None:
    """`steps` is [(name, text), ...]. Appended after the caller's atomic
    block has exited, through the one comment path, each keyed
    `<messageId>:<name>` so a redelivered webhook posts it once. Recorded
    complete only after it posts (local mode: the row's own
    `source_message_id` dedupes it; Jira mode: the writer's completion
    record does)."""
    for name, text in steps:
        store.append_comment(
            work_item_id, STORY_INTAKE_COMMENT_AUTHOR, text,
            source_message_id=f'{message_id}:{name}' if message_id else None,
            origin=write_gate.Origins.DIRECT,
        )


def _publish_side_effect(project: str, work_item_id, jira_issue_key: str, kind: str, detail: dict) -> None:
    """A small, Django-decided instruction, durably recorded for whichever
    consumer eventually acts on it. From v5.1 ScrumMaster's side-effect
    consumer (dispatchConsumer.js) acts only on a `blocked_cleared` kind,
    redispatching the assigned agent; `story_intake` — the kind this
    function's other caller, `_handle_story_created`, publishes — has no
    consumer at all until v5.2's outbound writer posts the acknowledgement
    or missing-fields comment it decides on. The DECISION (what happened,
    what should happen next) is made here regardless of which kind;
    executing it against Jira is deferred to that future writer. Must be
    called from inside an existing transaction.atomic() block so it lands
    atomically with whatever canonical write (if any) it accompanies."""
    store.write_outbox_event(
        project=project, event_type='work_item.jira_side_effect', work_item_id=work_item_id,
        payload={'kind': kind, 'externalKey': jira_issue_key, 'detail': detail},
    )


def _record_generic_event(project: str, work_item_id, jira_issue_key: str, envelope: dict, detail: dict) -> None:
    """Django durably records and republishes every ingested
    event as a canonical domain event, not only the subset that maps to
    an existing canonical work-item field. Catch-all for anything this
    module has no specific interpretation for yet (issue links, arbitrary
    changelog fields, unrecognized top-level event kinds, or an event
    whose issue key hasn't (yet) become a tracked canonical work item)."""
    with transaction.atomic():
        store.write_outbox_event(
            project=project, event_type='work_item.jira_event_received', work_item_id=work_item_id,
            payload={'jiraIssueKey': jira_issue_key, 'envelopeId': envelope.get('messageId'), 'detail': detail},
        )


def _issuetype_of(issue: dict) -> Optional[str]:
    return ((issue or {}).get('fields') or {}).get('issuetype', {}).get('name')


def _sync_story_detail(item: WorkItem, fields: dict) -> None:
    """Re-parse the story fields from a webhook's current `issue`
    snapshot and persist them, so a later edit in Jira (after this item's
    canonical record was first created) is reflected before any gate
    re-check. No-op for a non-story item."""
    if item.type != 'story':
        return
    detail = jira_interpret.parse_story_fields(fields)
    WorkItemStoryDetail.objects.update_or_create(
        work_item_id=item.id,
        defaults={
            'behavior': detail.get('behavior') or '',
            'acceptance_criteria': detail.get('acceptanceCriteria') or '',
            'constraints': detail.get('constraints') or '',
            'edge_cases': detail.get('edgeCases') or '',
            'out_of_scope': detail.get('outOfScope') or '',
            'value_hypothesis': detail.get('valueHypothesis'),
            'test_measurement': detail.get('testMeasurement'),
        },
    )


def _sync_release_detail(item: WorkItem, fields: dict) -> Optional[dict]:
    """Re-parse the release fields from a webhook's current `issue`
    snapshot and persist them — mirrors `_sync_story_detail`. No-op for a
    non-release item. Target Project is deliberately not written here: it
    maps onto `item.project` (set once, at materialization time — REQ-01),
    not a `work_item_release_detail` column.

    Returns the recorded candidate (`candidateSha`, `buildIdentifier`,
    `previewUrl`) when this sync recorded a candidate SHA that is non-empty
    and differs from the one `core` held, and None otherwise — the trigger
    for the recording step (release-mode-parity.md REQ-13), which the caller
    runs or not depending on whether this webhook materialized the
    Release."""
    if item.type != 'release':
        return None
    detail = jira_interpret.parse_release_fields(fields)
    held = WorkItemReleaseDetail.objects.filter(work_item_id=item.id).values_list('candidate_sha', flat=True).first()
    WorkItemReleaseDetail.objects.update_or_create(
        work_item_id=item.id,
        defaults={
            'release_notes': detail.get('releaseNotes'),
            'candidate_sha': detail.get('candidateSha'),
            'build_identifier': detail.get('buildIdentifier'),
            'preview_url': detail.get('previewUrl'),
        },
    )
    candidate_sha = detail.get('candidateSha')
    if candidate_sha and candidate_sha != held:
        return {'candidateSha': candidate_sha, 'buildIdentifier': detail.get('buildIdentifier'),
                'previewUrl': detail.get('previewUrl')}
    return None


def _sync_existing_release(item: WorkItem, fields: dict) -> None:
    """The Release sync for a webhook that did NOT materialize the Release
    (REQ-13, "The record"): the fields from the snapshot, and — when that
    recorded a new, non-empty candidate SHA — the candidate's recording
    step, `store.record_release_candidate_step`, the same step local mode's
    report runs. Must be called inside the caller's `transaction.atomic()`
    block, so the fields and the step's event commit together."""
    candidate = _sync_release_detail(item, fields)
    if candidate is not None:
        store.record_release_candidate_step(
            item, candidate_sha=candidate['candidateSha'], build_identifier=candidate['buildIdentifier'],
            preview_url=candidate['previewUrl'], author=RELEASE_AUTHOR,
        )


def _apply_validated_status_change(item: WorkItem, jira_status_name: str, issue_key: str, envelope: dict) -> None:
    """A Jira status change, applied through the project's own inbound map.

    A Jira status the project's map does not name has no canonical status
    to record, so **no canonical status change is made** (REQ-09's
    Acceptance: "a Jira-mode webhook moving an issue to a Jira status that
    only `DEFAULT_JIRA_STATUS_MAP` names changes no canonical status"). It
    is recorded as one webhook failure naming the work item and that Jira
    status, and nothing is raised, so the webhook is acknowledged as any
    other recorded-not-applied event is.

    One failure row rather than a generic event, because that is what this
    path already produced for an unnamed status up to v5.1 — the raw
    passthrough reached `status_vocabulary.validate_status`, which rejected
    it and landed here — and losing it would quietly remove the one place
    an operator sees that a project's status configuration does not cover
    what its Jira workflow offers. The reason now says that, instead of
    naming the canonical vocabulary. It is symmetric with the outbound
    side, where a push to a status the same map does not cover is recorded
    as one webhook failure too (§4)."""
    target_status = jira_status_to_canonical(item.project, jira_status_name)
    if target_status is None:
        record_failure(
            item.project, item.id, issue_key,
            f'Jira status "{jira_status_name}" is not mapped to a canonical status for this project — '
            'no canonical status was recorded',
            {'jiraStatusName': jira_status_name, 'envelopeId': envelope.get('messageId')},
        )
        return

    _apply_validated_status_change_to(
        item, target_status, issue_key, envelope, jira_status_name=jira_status_name,
    )


def _apply_validated_status_change_to(item: WorkItem, target_status: str, issue_key: str, envelope: dict,
                                       *, jira_status_name: Optional[str] = None) -> None:
    """The same validated-write-or-record-a-failure path, given the
    CANONICAL target directly rather than a Jira status name — what the
    Blocked flag's set branch needs, since the flag is not a Jira status
    and maps to `needs-clarification` by REQ-09's rule rather than through
    the project's status map."""
    try:
        store.transition_status(item.id, target_status, actor=f'jira-webhook:{issue_key}',
                                 origin=write_gate.Origins.JIRA_WEBHOOK)
    except Exception as err:
        # Rejected and recorded as a failure event
        # rather than applied — never treated as automatically
        # correct just because it originated in Jira.
        record_failure(item.project, item.id, issue_key, str(err),
                        {'jiraStatusName': jira_status_name, 'envelopeId': envelope.get('messageId')})


# ---------------------------------------------------------------------------
# Handler 1 (handlers.js `handleStoryCreated`) — jira:issue_created / Story
# ---------------------------------------------------------------------------

def _handle_story_created(project: str, issue: dict, issue_key: str, envelope: dict) -> None:
    fields = issue.get('fields') or {}
    detail = jira_interpret.parse_story_fields(fields)
    missing = jira_interpret.missing_story_fields(detail)
    display_name = fields.get('summary') or issue_key
    description = jira_interpret.adf_to_text(fields.get('description')).strip() or None
    status = 'proposed' if missing else 'ready'
    message_id = envelope.get('messageId')

    # This delivery's comment steps, in the order V1 posted them: the
    # acknowledgement always, then the missing-fields block when the
    # five-field gate refuses the Story (the "accepted with missing fields"
    # branch posts both). Derived from this envelope's own payload, so a
    # redelivery of the same message derives the same texts.
    steps = [('acknowledgement', STORY_INTAKE_ACKNOWLEDGEMENT)]
    if missing:
        steps.append(('missing-fields', _missing_fields_comment(missing)))

    with transaction.atomic():
        existing = WorkItem.objects.filter(external_key=issue_key).first()
        if existing is not None:
            # Already materialized. Up to v5.1 this was a bare no-op; from
            # v5.2 it first leaves this block and runs only the INCOMPLETE
            # comment steps a previous delivery of this same `messageId`
            # registered (REQ-09, "Canonical events with a Jira side
            # effect"), so a comment whose post failed is posted on
            # redelivery. A *first* delivery for a Story whose key already
            # has a row — one REQ-10's `connect_jira` pushed — registered
            # none, so it posts no story-intake comment at all.
            pending = set(jira_writer.pending_comment_steps(message_id, existing.id)) if message_id else set()
            resume = [(name, text) for name, text in steps if name in pending]
            work_item_id = existing.id
        else:
            item = store.create_work_item(
                {
                    'id': uuid.uuid4(), 'project': project, 'type': 'story',
                    'displayName': display_name, 'description': description,
                    'status': status, 'assigneeAgentId': 'refinement-agent',
                    'externalKey': issue_key, 'storyDetail': detail,
                },
                actor=f'jira-webhook:{issue_key}', origin=write_gate.Origins.JIRA_WEBHOOK,
            )
            # `ok` distinguishes the acknowledgement comment from the
            # missing-fields-block comment — the same choice V1's
            # handleStoryCreated made before posting. The writer consumes
            # this side effect for the Agent field and the Blocked flag;
            # the comments are appended below, not carried on the event.
            _publish_side_effect(project, item.id, issue_key, 'story_intake', {'ok': not missing, 'missing': missing})
            _register_comment_steps(message_id, item.id, [name for name, _ in steps])
            work_item_id = item.id
            resume = steps

    _post_comment_steps(work_item_id, message_id, resume)


# ---------------------------------------------------------------------------
# Handler 3 (handlers.js `handleBlockedCleared`) — the JIRA_BLOCKED_FIELD_ID
# custom field going from truthy to falsy.
# ---------------------------------------------------------------------------

# The statuses a Blocked flag set in Jira does not move. The writer pushes
# `needs-clarification` and `failed` as `set_blocked_field(true)` (v5.2
# REQ-09), so the flag coming back is read as `needs-clarification` — which
# is why `failed` is recorded as `needs-clarification` in Jira mode (§4) —
# and an item already in either keeps it. From v5.3 the writer writes
# `cancelled` as the Abandoned status instead (release-mode-parity.md
# REQ-14); a cancelled item still keeps its status when a person flags it,
# as it did before.
_BLOCKED_FLAG_BASELINES = ('needs-clarification', 'failed', 'cancelled')


def _handle_blocked_flag_set(project: str, issue_key: str, work_item_id, envelope: dict) -> None:
    """canonical-delivery-state.md REQ-09 — the Blocked field BECOMING SET
    is a validated transition to `needs-clarification`, origin
    JIRA_WEBHOOK, in Jira mode only (this module is reached for no other
    mode). Up to v5.1 nothing in the platform set the flag, so there was no
    "became blocked" webhook to react to; from v5.2 the writer sets it for
    every machine write of those three statuses, and a person can set it by
    hand, and both have to arrive here.

    Two cases keep their status: a work item in `proposed` — story intake's
    missing-fields block, which flags the ticket precisely to leave it
    `proposed` — and one already in one of the three statuses the flag
    stands for, which is the write having already taken effect."""
    if work_item_id is None:
        _record_generic_event(project, None, issue_key, envelope, {'field': 'blocked', 'set': True})
        return

    item = store.get_work_item(work_item_id)
    if not item:
        return

    baseline = status_vocabulary.baseline_of(
        item.status, project_config.get_custom_statuses(item.project),
    )
    if baseline == 'proposed' or baseline in _BLOCKED_FLAG_BASELINES:
        _record_generic_event(project, work_item_id, issue_key, envelope,
                               {'field': 'blocked', 'set': True, 'statusKept': item.status})
        return

    _apply_validated_status_change_to(item, 'needs-clarification', issue_key, envelope)


def _handle_blocked_flag_cleared_status(item: WorkItem, issue: dict, issue_key: str, envelope: dict) -> None:
    """REQ-09 — "When the flag is cleared, the work item first returns to
    the status its current Jira status maps to, and then the existing
    blocked-cleared handling runs." The webhook's `issue` snapshot carries
    the ticket's current status, which is where the item belongs now that
    the flag no longer overrides it."""
    jira_status_name = ((issue.get('fields') or {}).get('status') or {}).get('name')
    if not jira_status_name:
        return
    _apply_validated_status_change(item, jira_status_name, issue_key, envelope)


def _handle_blocked_field_change(project: str, issue: dict, issue_key: str, work_item_id, change: dict,
                                  envelope: dict) -> None:
    became_unblocked = bool(change.get('from')) and not change.get('to')
    became_blocked = (not change.get('from')) and bool(change.get('to'))

    if became_blocked:
        _handle_blocked_flag_set(project, issue_key, work_item_id, envelope)
        return
    if not became_unblocked:
        return  # neither direction — nothing this module interprets.

    if work_item_id is None:
        _record_generic_event(project, None, issue_key, envelope, {'field': 'blocked', 'cleared': True})
        return

    item = store.get_work_item(work_item_id)
    if not item:
        return

    _handle_blocked_flag_cleared_status(item, issue, issue_key, envelope)
    item = store.get_work_item(work_item_id)
    if not item:
        return

    custom_statuses = project_config.get_custom_statuses(item.project)
    baseline = status_vocabulary.baseline_of(item.status, custom_statuses)

    if item.type == 'story' and baseline == 'proposed':
        # A Jira webhook's `issue` payload carries the issue's CURRENT full
        # field snapshot at delivery time, not just the one field that
        # changed — re-sync the story's canonical fields from it before
        # retrying, so a human filling in Behavior/Acceptance Criteria/etc.
        # in Jira and then clearing Blocked is reflected here (the fields
        # captured at issue_created time would otherwise still read as
        # missing forever).
        with transaction.atomic():
            _sync_story_detail(item, issue.get('fields') or {})

        # Mirrors handleStoryCreated/handleBlockedCleared's refinement-agent
        # branch: re-run the exact same story-fields gate by attempting the same
        # transition a fresh Shovel-Ready-equivalent would attempt. Success
        # publishes the ordinary work_item.status_changed event, which is
        # all the dispatch consumer needs — no extra event required.
        try:
            with transaction.atomic():
                store.transition_status(item.id, 'ready', actor=f'jira-webhook:{issue_key}',
                                         origin=write_gate.Origins.JIRA_WEBHOOK)
        except Exception as err:
            record_failure(item.project, item.id, issue_key, str(err), {'reason': 'blocked_cleared_retry_failed'})
            story_detail = WorkItemStoryDetail.objects.filter(work_item_id=item.id).first()
            missing = jira_interpret.missing_story_fields({
                'behavior': story_detail.behavior if story_detail else None,
                'acceptanceCriteria': story_detail.acceptance_criteria if story_detail else None,
                'constraints': story_detail.constraints if story_detail else None,
                'edgeCases': story_detail.edge_cases if story_detail else None,
                'outOfScope': story_detail.out_of_scope if story_detail else None,
            })
            message_id = envelope.get('messageId')
            with transaction.atomic():
                _publish_side_effect(project, item.id, issue_key, 'story_intake', {'ok': False, 'missing': missing, 'reblock': True})
                _register_comment_steps(message_id, item.id, ['reblock'])
            # Appended after the block, like story intake's own comments and
            # for the same reason (REQ-09, "One comment path").
            _post_comment_steps(item.id, message_id, [('reblock', _reblock_comment(missing))])
        return

    # Everything else (a dev-agent ticket blocked mid-implementation): no
    # canonical status change — clearing Blocked must not itself move the
    # ticket's status. Just tell the dispatch consumer to redispatch this
    # item as a continuation (buildUnblockPrompt + BLOCKED-marker search,
    # matching handleBlockedCleared's non-refinement branch exactly).
    with transaction.atomic():
        _publish_side_effect(project, item.id, issue_key, 'blocked_cleared', {})


def _handle_agent_field_change(project: str, issue: dict, issue_key: str, work_item_id, envelope: dict) -> None:
    """canonical-delivery-state.md REQ-09, "An assignment -> `set_agent_field`":
    an Agent-field changelog item is a validated assignment, origin
    JIRA_WEBHOOK — `push_assignment`'s own write echoed back by Jira's
    webhook, or a person reassigning the ticket in Jira directly, treated
    the same way.

    The value is read from the webhook's current field snapshot with
    `jira_interpret.parse_agent_field`, not from the changelog item's
    `to`/`toString` — the same "the issue snapshot carries the ticket's
    FULL current field values" rule `_handle_blocked_flag_cleared_status`
    and `_sync_story_detail` already follow for their own fields.

    Validation reuses `store.assign_work_item`'s own rule
    (`_assert_assignment_valid` -> `assignment.validate_assignment`, the
    same catalog check `_subtask_assignee` runs for a mirrored Sub-task)
    rather than calling `validate_assignment` a second time here: a value
    outside the catalog surfaces as `AssignmentRejectedError`, caught below
    and recorded as one webhook failure, not applied. A cleared field
    (`parse_agent_field` returns falsy) is recorded as one webhook failure
    and not applied. A value equal to the item's current assignee is the
    push's own echo, or a no-op edit in Jira — nothing is recorded and
    nothing fails."""
    if work_item_id is None:
        _record_generic_event(project, None, issue_key, envelope, {'field': 'agent'})
        return

    item = store.get_work_item(work_item_id)
    if not item:
        return

    agent_id = jira_interpret.parse_agent_field(issue.get('fields') or {})

    if not agent_id:
        record_failure(
            project, work_item_id, issue_key,
            f'{issue_key}: the Agent field was cleared — assignment unchanged',
            {'event': 'agent_field', 'envelopeId': envelope.get('messageId')},
        )
        return

    if agent_id == item.assignee_agent_id:
        return  # push_assignment's own write, echoed back, or a no-op edit.

    try:
        store.assign_work_item(work_item_id, agent_id, actor=f'jira-webhook:{issue_key}',
                                origin=write_gate.Origins.JIRA_WEBHOOK)
    except store.AssignmentRejectedError as err:
        result = err.result
        record_failure(
            project, work_item_id, issue_key,
            f'{issue_key}: Agent "{agent_id}" is not a valid assignee for this project ({result.get("code")})',
            {'event': 'agent_field', 'requestedAgent': agent_id,
             'permittedAgents': result.get('permittedAgents'),
             'envelopeId': envelope.get('messageId')},
        )


# ---------------------------------------------------------------------------
# Comments and Release events — see module docstring for the
# Release scope carve-out.
# ---------------------------------------------------------------------------

# `[<author>] <body>` — the prefix the writer posts so an author survives
# the round trip (REQ-09, "The author survives the round trip"). Stripped
# back off here, and ONLY for a comment Jira says came from the configured
# integration account, so a person who happens to start a comment with a
# bracketed word keeps their own author.
_AUTHORED_PREFIX = re.compile(r'^\[([^\]\n]+)\]\s(.*)$', re.DOTALL)


def _author_round_trip(author: str, text: str, comment: dict) -> tuple[str, str]:
    integration_email = (os.environ.get('JIRA_EMAIL') or '').strip()
    author_email = ((comment.get('author') or {}).get('emailAddress') or '').strip()
    if not integration_email or author_email.lower() != integration_email.lower():
        return author, text
    match = _AUTHORED_PREFIX.match(text)
    if not match:
        return author, text
    return match.group(1), match.group(2)


def _handle_comment_event(project: str, issue_key: str, body: dict, resolve_work_item_id, envelope: dict) -> None:
    comment = body.get('comment') or {}
    work_item_id = resolve_work_item_id(issue_key)
    author = (comment.get('author') or {}).get('displayName') or 'Unknown'
    text = jira_interpret.adf_to_text(comment.get('body')).strip()
    comment_id = comment.get('id')
    author, text = _author_round_trip(author, text, comment)

    if work_item_id is None:
        _record_generic_event(project, None, issue_key, envelope, {'event': 'comment', 'author': author, 'body': text})
        return

    # REQ-10: the comment's ticket may resolve (via
    # `default_resolve_work_item_id`) to an existing work item whose own
    # project isn't in Jira mode — ignored like the entry-level check,
    # applying nothing to that item.
    item = store.get_work_item(work_item_id)
    if item is not None and project_config.get_mode(item.project)['mode'] != project_config.JIRA:
        _record_generic_event(project, work_item_id, issue_key, envelope,
                               {'event': 'comment', 'author': author, 'body': text,
                                'ignored': 'work item project not in Jira mode'})
        return

    # Origin JIRA_WEBHOOK, so `write_gate.route` answers `record`: this is
    # the write path a Jira comment takes INTO `core`, in Jira mode as in
    # local mode, and the same `store.append_comment` the writer's own
    # posted comment comes back through (REQ-09, "One comment path").
    with transaction.atomic():
        store.append_comment(
            work_item_id, author, text,
            source_message_id=f'jira-comment:{issue_key}:{comment_id}' if comment_id else None,
            origin=write_gate.Origins.JIRA_WEBHOOK,
        )


def _materialize_release(project: str, issue: dict, issue_key: str,
                          envelope: dict) -> tuple[Optional[WorkItem], bool]:
    """Creates the canonical `release` work item and its
    `work_item_release_detail` row from a Release ticket's current field
    snapshot, if one doesn't already exist for `issue_key` — idempotent on
    `external_key`, same shape as `_handle_story_created`'s redelivery
    guard — then (re)syncs the detail row from `issue['fields']` either
    way, so a caller that already had an item still picks up any fields
    it hasn't seen yet. Under REQ-10 it can decline both halves: it
    refuses to create a row at all when the ticket's Target Project (or its
    own project, absent one) is not in Jira mode, recording a webhook
    failure instead; and for a ticket that already resolves to a work item,
    it refuses to (re)sync the detail row when that item's own project is
    not in Jira mode, recording a generic event instead and returning
    `None`. Shared by `_handle_release_requested`
    (`jira:issue_created`) and `_handle_changelog_item` (an update webhook
    for a Release ticket that was never materialized — BUGFIXES.md BF-01
    Pass 1 audit row 2, REQ-01 "create or update webhook"). Must be called
    from inside an existing `transaction.atomic()` block, same requirement
    as `_publish_side_effect`.

    Returns `(item, created)`: `created` is True only when this call
    created the Release (release-mode-parity.md REQ-10, REQ-11, REQ-13) —
    only that webhook may publish `requested`, and only a webhook that did
    NOT create it runs the candidate's recording step on a changed SHA, so
    a Release created with a Candidate SHA already set records it as a field
    and publishes no `work_item.release_candidate_recorded`."""
    fields = issue.get('fields') or {}
    detail = jira_interpret.parse_release_fields(fields)
    display_name = fields.get('summary') or issue_key
    # Target Project maps onto the work item's own `project`
    # (REQ-01) — falls back to the ticket's own containing Jira project if
    # the custom field isn't configured or set, same fallback order
    # views.py uses for issue.fields.project itself.
    target_project = registry.normalize_project_name(
        detail.get('targetProjectName') or detail.get('targetProjectKey') or project
    )

    item = WorkItem.objects.filter(external_key=issue_key).first()
    if item is not None:
        # REQ-10: the ticket already resolves to a work item — a Release
        # filed under a local-mode Target Project before this mode check
        # existed, or one whose Target Project was switched to local
        # since. Its own project's mode governs, not the envelope's:
        # ignored like the entry-level check, not a failure.
        if project_config.get_mode(item.project)['mode'] != project_config.JIRA:
            _record_generic_event(project, item.id, issue_key, envelope,
                                   {'event': 'release_materialize', 'ignored': 'resolved work item project not in Jira mode'})
            return None, False
        _sync_existing_release(item, fields)
        return item, False

    # REQ-10: a Jira Release is never filed under a Target Project not in
    # Jira mode.
    if project_config.get_mode(target_project)['mode'] != project_config.JIRA:
        record_failure(target_project, None, issue_key, 'release target project not in Jira mode', fields)
        return None, False

    item = store.create_work_item(
        {
            'id': uuid.uuid4(), 'project': target_project, 'type': 'release',
            'displayName': display_name, 'status': 'proposed', 'externalKey': issue_key,
        },
        actor=f'jira-webhook:{issue_key}', origin=write_gate.Origins.JIRA_WEBHOOK,
    )
    _sync_release_detail(item, fields)
    return item, True


def _target_project_set(issue: dict) -> bool:
    """Whether the Release ticket's own Target Project field is set, read
    raw from `parse_release_fields` — before `_materialize_release`'s
    fallback to the ticket's own project, which `item.project` already
    carries (v5.2 REQ-08, kept by release-mode-parity.md REQ-11)."""
    detail = jira_interpret.parse_release_fields(issue.get('fields') or {})
    return bool(detail.get('targetProjectName') or detail.get('targetProjectKey'))


RELEASE_CHECK_STEP = 'release-check'
RELEASE_IN_PROGRESS_STEP = 'release-in-progress'


def _handle_release_requested(project: str, issue: dict, issue_key: str, envelope: dict) -> None:
    """A Jira-mode release request: a Release ticket's `jira:issue_created`
    (release-mode-parity.md REQ-09 … REQ-11).

    Inside one `transaction.atomic()` block, and only when this webhook
    materializes the Release (`_materialize_release`, BF-01):

      - the one release gate, `store.release_request_refusal`, with whether
        the ticket's own Target Project field is set (REQ-11);
      - when it passes, `requested` through `store.publish_release_event` on
        the ticket's own project's stream (REQ-10), so the Release and its
        event commit together, and the In Progress push registered as the
        writer step `<messageId>:release-in-progress` (REQ-09);
      - when it refuses, the comment registered as the step
        `<messageId>:release-check`.

    After the block: the refusal posted with `store.post_release_rejection`,
    and the In Progress push made by `jira_writer.push_release_in_progress`
    — each only while its step is registered and not complete.

    A redelivered creation webhook materializes nothing, so it runs neither
    check again and publishes nothing; it runs only the steps a previous
    delivery registered and did not complete. A requested Release therefore
    never receives a rejection, a refused one is never pushed In Progress,
    and a replay after the candidate moves nothing (REQ-09). The text of a
    rejection whose post failed is re-derived for its retry from the same
    read-only gate, which writes nothing.

    `_materialize_release` returns no item, and this function publishes and
    comments nothing, when v5.2 REQ-10's mode check declines the Release."""
    message_id = envelope.get('messageId')
    check_key = f'{message_id}:{RELEASE_CHECK_STEP}' if message_id else None
    progress_key = f'{message_id}:{RELEASE_IN_PROGRESS_STEP}' if message_id else None
    target_project_set = _target_project_set(issue)
    refusal = None
    resume_check = False

    with transaction.atomic():
        item, created = _materialize_release(project, issue, issue_key, envelope)
        if item is None:
            return

        if created:
            refusal = store.release_request_refusal(item, target_project_set=target_project_set)
            if refusal is None:
                store.publish_release_event(item, 'requested', stream_project=project)
                if progress_key:
                    jira_writer.register_step(progress_key, item.id, jira_writer.STEP_STATUS)
            elif check_key:
                jira_writer.register_step(check_key, item.id, jira_writer.STEP_COMMENT)
        else:
            resume_check = jira_writer.is_step_pending(check_key, item.id, jira_writer.STEP_COMMENT)

    if resume_check:
        refusal = store.release_request_refusal(item, target_project_set=target_project_set)
        if refusal is None:
            # The cause has cleared since the post failed; there is nothing
            # left to say, and the Release stays `proposed` either way.
            jira_writer.mark_step_complete(check_key, item.id, jira_writer.STEP_COMMENT)

    if refusal is not None:
        store.post_release_rejection(item, refusal, source_message_id=check_key)

    jira_writer.push_release_in_progress(item, completion_key=progress_key)


def _apply_release_terminal_status(project: str, item: WorkItem, target_status: str, kind: str,
                                    issue_key: str, envelope: dict, jira_status_name: str) -> None:
    """A Jira-mode Release's move to `done` or `cancelled`, and its release
    event, in ONE `transaction.atomic()` block (release-mode-parity.md
    REQ-10), so a crash between them loses neither: a `done` recorded
    without its event would never be promoted, since its redelivery changes
    nothing.

    Only a webhook that CHANGES the Release's canonical status publishes. A
    Release `core` already holds at the target publishes nothing — which is
    what makes `connect_jira`'s re-sync of a done or cancelled Release safe
    (REQ-14) — while a Release moved to Done, reopened and moved to Done
    again publishes `done` each time.

    The change goes through `store.transition_status`, origin JIRA_WEBHOOK,
    which publishes no event of its own (v5.2 REQ-08's guard). A refused
    change is recorded as one webhook failure and publishes nothing, as
    every other validated Jira status change is."""
    with transaction.atomic():
        current = WorkItem.objects.select_for_update().filter(id=item.id).first()
        if current is None or current.status == target_status:
            return
        try:
            with transaction.atomic():
                store.transition_status(item.id, target_status, actor=f'jira-webhook:{issue_key}',
                                         origin=write_gate.Origins.JIRA_WEBHOOK)
        except Exception as err:  # noqa: BLE001 - recorded, as _apply_validated_status_change_to does
            record_failure(item.project, item.id, issue_key, str(err),
                            {'jiraStatusName': jira_status_name, 'envelopeId': envelope.get('messageId')})
            return
        store.publish_release_event(current, kind, stream_project=project)


def _handle_release_done(project: str, item: Optional[WorkItem], issue_key: str, envelope: dict,
                          jira_status_name: str) -> None:
    """A Release ticket's move to Done — the single production-approval gate
    (v5.2 REQ-08) — records `done` and publishes `done` together, only on a
    change (REQ-10). `item` is `None` only if the Release was never
    materialized AND `_materialize_release` just declined it too (v5.2
    REQ-10): recorded as a failure rather than silently dropped."""
    if item is None:
        record_failure(project, None, issue_key,
                        'Release moved to Done with no canonical release work item resolved', None)
        return
    _apply_release_terminal_status(project, item, 'done', 'done', issue_key, envelope, jira_status_name)


def _handle_release_abandoned(project: str, item: Optional[WorkItem], issue_key: str, envelope: dict,
                               jira_status_name: str) -> None:
    """A Release ticket's move to Abandoned (release-mode-parity.md REQ-14)
    records `cancelled` and publishes `abandoned` together, only on a change
    (REQ-10), so ScrumMaster tears down its preview and nothing is promoted.
    See `_handle_release_done` on `item is None`."""
    if item is None:
        record_failure(project, None, issue_key,
                        'Release abandoned with no canonical release work item resolved', None)
        return
    _apply_release_terminal_status(project, item, 'cancelled', 'abandoned', issue_key, envelope,
                                    jira_status_name)


_RELEASE_TERMINAL_HANDLERS = {'done': _handle_release_done, 'cancelled': _handle_release_abandoned}


# ---------------------------------------------------------------------------
# The Sub-task and link mirror (canonical-delivery-state.md REQ-11)
# ---------------------------------------------------------------------------
#
# The inbound half of Jira-mode decomposition. REQ-09's writer creates a Jira
# Sub-task per proposal and records its key; this is what turns each of those
# issues — and each Sub-task a PERSON creates in Jira, and each Blocks link a
# person draws — into canonical state, so that from then on "Jira-mode
# dependency progression and dispatch are canonical, as in local mode, except
# that Jira moves first".
#
# Everything here runs in the inbound tracker layer, origin JIRA_WEBHOOK, so
# `write_gate.route` answers `record`: this is the one write path into `core`
# in Jira mode. The one thing it does NOT record is a derived `ready` — that
# goes to Jira first, as every derived write does (REQ-09, "Derived writes go
# through the router too"), and comes back on the issue's own webhook.

# A Backlog subtask is the one whose status is derived rather than mapped
# (REQ-11). "Backlog" is read through the project's own inbound map rather
# than matched literally, so a project whose `ProjectStatusConfig` rows name
# its first column something else is handled by its own configuration; with
# `DEFAULT_JIRA_STATUS_MAP` it is exactly `Backlog`.
_BACKLOG_CANONICAL = 'proposed'

# The two canonical statuses a Backlog subtask can hold, and so the ones a
# re-derivation may act on: `proposed` (no blocker, or none known yet) and
# `waiting-on-dependency` (a blocker that is not done). A subtask at any
# other status is one Jira has moved past Backlog, or one a person moved
# back from Shovel Ready, which "keeps the status its Jira status maps to"
# (Shovel Ready Pass 6, decision 5.1).
_DERIVABLE_STATUSES = (_BACKLOG_CANONICAL, 'waiting-on-dependency')


def _canonical_for_key(issue_key: Optional[str]) -> Optional[WorkItem]:
    if not issue_key:
        return None
    return WorkItem.objects.filter(external_key=issue_key).first()


def _subtask_parent(project: str, issue: dict, issue_key: str, envelope: dict) -> Optional[WorkItem]:
    """The canonical parent of a Jira Sub-task, "found by the parent issue's
    `external_key`". A parent with no canonical row is one webhook failure
    and the subtask is mirrored with no parent (REQ-11) — it is a real
    subtask either way, and leaving it unmirrored would lose it."""
    parent_key = ((issue.get('fields') or {}).get('parent') or {}).get('key')
    parent = _canonical_for_key(parent_key)
    if parent is None:
        record_failure(
            project, None, issue_key,
            f'Sub-task {issue_key}: parent issue {parent_key or "(none on the issue)"} has no canonical '
            'work item — the subtask is mirrored with no parent',
            {'event': 'subtask_mirror', 'parentIssueKey': parent_key,
             'envelopeId': envelope.get('messageId')},
        )
    return parent


def _subtask_assignee(project: str, fields: dict, issue_key: str, envelope: dict) -> Optional[str]:
    """The subtask's assignee, "from the Agent field, validated against the
    agent catalog (a missing or unknown agent is recorded as a webhook
    failure, and the subtask is mirrored unassigned)" (REQ-11). Validated by
    the same catalog-backed rules every assignment goes through
    (`assignment.validate_assignment`), never by what Jira's field happens
    to offer."""
    agent_id = jira_interpret.parse_agent_field(fields)
    if not agent_id:
        record_failure(
            project, None, issue_key,
            f'Sub-task {issue_key}: the Agent field is not set — the subtask is mirrored unassigned',
            {'event': 'subtask_mirror', 'envelopeId': envelope.get('messageId')},
        )
        return None

    verdict = assignment.validate_assignment(project, agent_id)
    if not verdict['ok']:
        record_failure(
            project, None, issue_key,
            f'Sub-task {issue_key}: Agent "{agent_id}" is not a valid assignee for this project '
            f'({verdict["code"]}) — the subtask is mirrored unassigned',
            {'event': 'subtask_mirror', 'requestedAgent': agent_id,
             'permittedAgents': verdict.get('permittedAgents'),
             'envelopeId': envelope.get('messageId')},
        )
        return None
    return agent_id


def _add_record_links_for(item: WorkItem, issue_key: str) -> None:
    """Every Blocks pair the decomposition record holds in which this
    subtask's key is either end, added as a canonical `blocks` link, for the
    pairs whose other end already has a row (Shovel Ready Pass 6, decision
    2.1). Idempotent per pair: `store.create_link` dedupes an existing
    edge.

    This is what closes REQ-11's ordering window from the other side: a
    link event that arrived before this subtask existed was skipped with no
    failure precisely because the record holds it, and this is where it is
    honoured."""
    for pair in jira_writer.record_pairs_naming_key(issue_key):
        blocker = _canonical_for_key(pair.get('blockerKey')) or \
            WorkItem.objects.filter(id=pair.get('blockerProposalId')).first()
        dependent = _canonical_for_key(pair.get('dependentKey')) or \
            WorkItem.objects.filter(id=pair.get('dependentProposalId')).first()
        if blocker is None or dependent is None or blocker.id == dependent.id:
            continue
        store.create_link(blocker.id, dependent.id, 'blocks',
                           actor=f'jira-webhook:{issue_key}', origin=write_gate.Origins.JIRA_WEBHOOK)


def _derived_backlog_status(item: WorkItem) -> Optional[str]:
    """A Backlog subtask's status, "derived from its inward blockers:
    `waiting-on-dependency` while any is not `done`, `ready` once it has at
    least one and all are `done`, and `proposed` with none" (REQ-11).

    The inward blockers are `store.inward_blockers_of`'s union of canonical
    `blocks` links and the decomposition record's Blocks pairs, where a pair
    whose blocker has no row yet counts as not `done`."""
    blockers = store.inward_blockers_of(item)
    if not blockers:
        return _BACKLOG_CANONICAL
    if all(blocker['blocker_status'] == 'done' for blocker in blockers):
        return 'ready'
    return 'waiting-on-dependency'


def _push_derived_ready(item: WorkItem) -> None:
    """A derived `ready` is not recorded: it is pushed to Jira as the Jira
    status the project's map writes for `ready` (Shovel Ready with the
    default map), origin `ROLLUP`, and `core` records `ready` from that
    issue's own webhook (REQ-09, "Derived writes go through the router
    too"; REQ-11).

    `route` is asked rather than assumed, like every other write: the
    mirror is only ever reached for a Jira-mode project, so the answer is
    `push`, but no caller in this service states a mode."""
    if write_gate.route(item.project, write_gate.Origins.ROLLUP) == write_gate.PUSH:
        jira_writer.register_derived_status_push(item, 'ready')


def mirror_subtask(project: str, issue: dict, issue_key: str, envelope: dict) -> Optional[WorkItem]:
    """REQ-11 — materializes a Jira `Sub-task` as a canonical work item of
    type `task`, origin JIRA_WEBHOOK, on `jira:issue_created` or on the
    first `jira:issue_updated`.

    **Skipped when a canonical row already holds its key** — a redelivered
    create, or a Sub-task `connect_jira` pushed and keyed. The existing row
    is returned so the caller can still reconcile its links.

    Its canonical id is "the proposal id the record holds for its key ... as
    local mode does, or a fresh id when the record holds none": a Sub-task
    the writer created gets the same canonical id local mode would have
    given it, and one a person created in Jira gets a fresh one. (§4 records
    the window where a Sub-task's webhook beats the writer's key record, in
    which case it gets a fresh id too.)

    Its status "maps from its Jira status, except that a `Backlog` subtask
    is derived from its inward blockers". It is "created and transitioned in
    one transaction, so `_recompute_parent_rollup` and `_unblock_dependents`
    run" — a subtask first seen at Done rolls its parent up on the spot."""
    existing = _canonical_for_key(issue_key)
    if existing is not None:
        return existing

    fields = issue.get('fields') or {}
    record = jira_writer.proposal_record_for_key(issue_key)
    canonical_id = record.proposal_id if record is not None else uuid.uuid4()
    display_name = fields.get('summary') or issue_key
    description = jira_interpret.adf_to_text(fields.get('description')).strip() or None
    parent = _subtask_parent(project, issue, issue_key, envelope)
    assignee = _subtask_assignee(project, fields, issue_key, envelope)

    jira_status = ((fields.get('status') or {}).get('name')) or ''
    mapped = jira_status_to_canonical(project, jira_status)
    if mapped is None:
        record_failure(
            project, None, issue_key,
            f'Sub-task {issue_key}: Jira status "{jira_status}" is not mapped to a canonical status for '
            'this project — the subtask is mirrored as "proposed"',
            {'event': 'subtask_mirror', 'jiraStatusName': jira_status,
             'envelopeId': envelope.get('messageId')},
        )
        mapped = _BACKLOG_CANONICAL

    derived_ready = False
    with transaction.atomic():
        item = store.create_work_item(
            {
                'id': canonical_id, 'project': project, 'type': 'task',
                'displayName': display_name, 'description': description,
                'status': _BACKLOG_CANONICAL, 'externalKey': issue_key,
                'parentId': parent.id if parent is not None else None,
                'assigneeAgentId': assignee,
            },
            actor=f'jira-webhook:{issue_key}', origin=write_gate.Origins.JIRA_WEBHOOK,
        )
        # The record's own Blocks pairs first, so the derivation below sees
        # the blockers this subtask was created with.
        _add_record_links_for(item, issue_key)
        item = store.get_work_item(item.id)

        target = mapped
        if mapped == _BACKLOG_CANONICAL:
            derived = _derived_backlog_status(item)
            if derived == 'ready':
                # Not recorded here: "the mirror records the mapped status,
                # and the routing layer pushes ... `ready` ... after the
                # mirror's transaction commits".
                derived_ready = True
                target = _BACKLOG_CANONICAL
            else:
                target = derived

        if target != item.status:
            # In the SAME transaction as the create, so the parent rollup and
            # the dependency unblock both run for a subtask first seen at a
            # later status.
            #
            # Through the inbound layer's own validated-write-or-record-a-
            # failure path, not a bare `transition_status`: a Sub-task a
            # person moved to Shovel Ready while a blocker of it is not
            # `done` maps to a canonical status the dependency gate refuses,
            # and a raise here would roll the whole mirror back and fail the
            # webhook message for ever. The subtask is mirrored either way
            # and the refusal is recorded, which is what this path does for
            # every other Jira-originated status it cannot apply.
            _apply_validated_status_change_to(item, target, issue_key, envelope,
                                               jira_status_name=jira_status)

        # The mirror's side of REQ-11's "whichever lands second attaches":
        # one implementation, in the writer, called from both sides.
        if record is not None:
            jira_writer.attach_record_references(record)

    item = store.get_work_item(canonical_id)
    if derived_ready and item is not None:
        _push_derived_ready(item)
    return item


def _snapshot_blocks_pairs(issue: dict) -> list[tuple]:
    """The Blocks pairs an issue's own `issuelinks` snapshot holds, as
    `(blocker_key, dependent_key)`.

    The inward/outward reading is `jira_client.get_issue_links`'s, so the
    two agree on direction: an entry carrying `inwardIssue` names an issue
    this one is blocked BY, and one carrying `outwardIssue` names an issue
    this one blocks. Only the configured Blocks link type counts."""
    fields = issue.get('fields') or {}
    links = fields.get('issuelinks') or []
    if not links:
        return []

    own_key = issue.get('key')
    link_type_id = str(jira_client.get_blocks_link_type_id())
    pairs = []
    for link in links:
        if str((link.get('type') or {}).get('id')) != link_type_id:
            continue
        inward = (link.get('inwardIssue') or {}).get('key')
        if inward:
            pairs.append((inward, own_key))
        outward = (link.get('outwardIssue') or {}).get('key')
        if outward:
            pairs.append((own_key, outward))
    return pairs


def _reconcile_pair(project: str, blocker_key: str, dependent_key: str, issue_key: str,
                     envelope: dict) -> tuple[Optional[WorkItem], Optional[WorkItem], bool]:
    """One Blocks pair, reconciled into a canonical `blocks` link, origin
    JIRA_WEBHOOK, idempotent per pair (REQ-11). Reconciliation only ADDS
    links.

    "A link whose other end has no canonical row is skipped. It is recorded
    as a webhook failure unless the record holds it as a Blocks pair, which
    the mirror adds when that end is created" (Shovel Ready Pass 6, decision
    2.1; Pass 7, SR-7-11) — a decomposition's own link is not a defect just
    because its subtask's create webhook has not been processed yet.

    Returns `(blocker, dependent, added)`, where `added` is True only when
    this call created the canonical link."""
    blocker = _canonical_for_key(blocker_key)
    dependent = _canonical_for_key(dependent_key)
    if blocker is None or dependent is None:
        if not jira_writer.record_holds_pair(blocker_key, dependent_key):
            record_failure(
                project, None, issue_key,
                f'Blocks link {blocker_key} -> {dependent_key}: '
                f'{"blocker" if blocker is None else "dependent"} has no canonical work item — '
                'the link is skipped',
                {'event': 'blocks_link_reconciliation', 'blockerKey': blocker_key,
                 'dependentKey': dependent_key, 'envelopeId': envelope.get('messageId')},
            )
        return blocker, dependent, False

    if blocker.id == dependent.id:
        return blocker, dependent, False

    result = store.create_link(blocker.id, dependent.id, 'blocks',
                                actor=f'jira-webhook:{issue_key}',
                                origin=write_gate.Origins.JIRA_WEBHOOK)
    return blocker, dependent, not result.get('deduped')


def _rederive_ends(project: str, ends: list, newly_linked_ids: set, issue_key: str,
                    envelope: dict) -> None:
    """Both ends of every reconciled pair, re-derived under REQ-11's Backlog
    rule — but "only for a subtask whose canonical status is
    `waiting-on-dependency` or for which this reconciliation added a Blocks
    pair", so "a Backlog subtask a person moved back keeps the status its
    Jira status maps to" and receives no Shovel Ready push (Shovel Ready
    Pass 6, decision 5.1).

    A derived `waiting-on-dependency` is RECORDED, origin JIRA_WEBHOOK —
    no Jira status maps to it with the default map, and it is the inbound
    layer's own reading of a Backlog subtask with an unfinished blocker. A
    derived `ready` is PUSHED through `route`, origin ROLLUP, after this
    function's caller has left its transaction, and recorded only from the
    issue's own webhook.

    A subtask's canonical status being `proposed` or
    `waiting-on-dependency` is what stands for "its Jira status is
    Backlog": those are the only two canonical statuses a Backlog subtask
    holds, and any other means Jira has it past Backlog."""
    to_push = []
    seen = set()
    for item_id in ends:
        if item_id is None or str(item_id) in seen:
            continue
        seen.add(str(item_id))
        item = store.get_work_item(item_id)
        # Subtasks only: REQ-11's derivation is a Sub-task rule, and a
        # mirrored Sub-task is a canonical `task`, as `materialize.py`
        # creates one.
        if item is None or item.type != 'task' or item.status not in _DERIVABLE_STATUSES:
            continue
        if item.status != 'waiting-on-dependency' and str(item_id) not in newly_linked_ids:
            continue

        derived = _derived_backlog_status(item)
        if derived == 'ready':
            to_push.append(item)
        elif derived == 'waiting-on-dependency' and item.status != 'waiting-on-dependency':
            _apply_validated_status_change_to(item, 'waiting-on-dependency', issue_key, envelope)

    for item in to_push:
        _push_derived_ready(store.get_work_item(item.id) or item)


def reconcile_subtask_snapshot(project: str, issue: dict, issue_key: str, envelope: dict) -> None:
    """"On every `jira:issue_updated` for a subtask, `webhook_consumer.py`
    MUST reconcile the snapshot's Blocks links into canonical `blocks`
    links, origin `JIRA_WEBHOOK`, idempotent per pair, and then re-derive
    both ends' status under the rule above" (REQ-11)."""
    pairs = _snapshot_blocks_pairs(issue)
    if not pairs:
        return

    ends = []
    newly_linked_ids: set = set()
    for blocker_key, dependent_key in pairs:
        blocker, dependent, added = _reconcile_pair(project, blocker_key, dependent_key, issue_key, envelope)
        for end in (blocker, dependent):
            if end is not None:
                ends.append(end.id)
                if added:
                    newly_linked_ids.add(str(end.id))

    _rederive_ends(project, ends, newly_linked_ids, issue_key, envelope)


def handle_issue_link_created(project: str, body: dict, envelope: dict) -> None:
    """Jira's own link-creation event (REQ-11). Its body "carries
    `issueLink` and no `issue`", which is why `views.py` routes it by the
    source issue's project and `handle_webhook_envelope` dispatches it
    ahead of its `issue_key` guard.

    "If `issueLink.issueLinkType.id` is the Blocks type
    (`get_blocks_link_type_id`), `core` reconciles that one pair, source
    blocking destination, reading each end's key with `get_issue`, which
    returns no links." Reconciliation is idempotent per pair, so whichever
    of this event and the dependent's next `jira:issue_updated` arrives
    first does the work."""
    link = body.get('issueLink') or {}
    link_type_id = (link.get('issueLinkType') or {}).get('id')
    if not link_type_id or str(link_type_id) != str(jira_client.get_blocks_link_type_id()):
        _record_generic_event(project, None, None, envelope,
                               {'event': 'issuelink_created', 'issueLinkId': link.get('id'),
                                'ignored': 'not the Blocks issue link type'})
        return

    source_id = link.get('sourceIssueId')
    destination_id = link.get('destinationIssueId')
    if not source_id or not destination_id:
        _record_generic_event(project, None, None, envelope,
                               {'event': 'issuelink_created', 'issueLinkId': link.get('id'),
                                'ignored': 'issueLink carries no source or destination issue id'})
        return

    blocker_key = (jira_client.get_issue(str(source_id)) or {}).get('key')
    dependent_key = (jira_client.get_issue(str(destination_id)) or {}).get('key')
    blocker, dependent, added = _reconcile_pair(
        project, blocker_key, dependent_key, dependent_key or blocker_key or '', envelope,
    )
    ends = [end.id for end in (blocker, dependent) if end is not None]
    newly_linked_ids = {str(item_id) for item_id in ends} if added else set()
    _rederive_ends(project, ends, newly_linked_ids, dependent_key or blocker_key or '', envelope)


# ---------------------------------------------------------------------------
# Top-level dispatch
# ---------------------------------------------------------------------------

def _handle_issue_created(project: str, issue: dict, issue_key: str, envelope: dict) -> None:
    issuetype = _issuetype_of(issue)
    if issuetype == 'Story':
        _handle_story_created(project, issue, issue_key, envelope)
    elif issuetype == 'Release':
        _handle_release_requested(project, issue, issue_key, envelope)
    elif issuetype == 'Sub-task':
        # canonical-delivery-state.md REQ-11 — the Sub-task mirror. Skipped
        # when a canonical row already holds the key (a redelivered create,
        # or a Sub-task `connect_jira` pushed and keyed), and the issue's
        # own Blocks links are reconciled either way, so whichever of the
        # create and the link event arrives first does the work.
        item = mirror_subtask(project, issue, issue_key, envelope)
        if item is not None:
            reconcile_subtask_snapshot(project, issue, issue_key, envelope)
    else:
        _record_generic_event(project, None, issue_key, envelope, {'event': 'issue_created', 'issuetype': issuetype})


def _handle_changelog_item(project: str, issue: dict, issue_key: str, work_item_id, change: dict,
                            envelope: dict) -> None:
    field = change.get('field')
    issuetype = _issuetype_of(issue)
    item = store.get_work_item(work_item_id) if work_item_id is not None else None

    # REQ-10: the ticket may resolve (via `default_resolve_work_item_id`)
    # to an existing work item whose own project isn't in Jira mode —
    # ignored like the entry-level check, applying nothing to that item.
    if item is not None and project_config.get_mode(item.project)['mode'] != project_config.JIRA:
        _record_generic_event(project, work_item_id, issue_key, envelope,
                               {'field': field, 'ignored': 'work item project not in Jira mode'})
        return

    if issuetype == 'Release' and item is None:
        # An update webhook for a Release ticket that was never
        # materialized — e.g. one created before BF-01 shipped, whose
        # jira:issue_created webhook came and went with no canonical
        # `release` work item to show for it. REQ-01 requires a "create
        # OR update" webhook to materialize it (BUGFIXES.md BF-01 Pass 1
        # audit row 2); without this branch, every later update for that
        # ticket falls through to `_record_generic_event` forever.
        # `_materialize_release` is idempotent on `external_key`, the same
        # helper `_handle_release_requested` uses for the create path. A
        # Release it creates here records its fields and runs no recording
        # step (release-mode-parity.md REQ-13).
        with transaction.atomic():
            item, _created = _materialize_release(project, issue, issue_key, envelope)
        work_item_id = item.id if item is not None else None
    elif issuetype == 'Release' and item is not None and item.type == 'release':
        # The webhook's `issue` snapshot always carries the ticket's FULL
        # current field values, not just the one `change` names — re-sync
        # once per changelog item (idempotent; same value if nothing
        # release-related changed) so a Candidate SHA/Build
        # Identifier/Preview URL edit lands regardless of which changelog
        # field the webhook happens to report it under — and, on a new
        # candidate SHA, the recording step runs in the same transaction
        # (release-mode-parity.md REQ-13). Idempotent across the webhook's
        # changelog items: only the first sees the SHA change.
        with transaction.atomic():
            _sync_existing_release(item, issue.get('fields') or {})
    elif issuetype == 'Sub-task':
        # canonical-delivery-state.md REQ-11. Two things, in this order,
        # before the field-specific handling below:
        #
        #   - a Sub-task with no canonical row is mirrored here, on "the
        #     first `jira:issue_updated`" — the same create-or-update rule
        #     the Release branch above follows, and without it a Sub-task
        #     whose `jira:issue_created` came and went unmirrored would
        #     never become canonical;
        #   - the snapshot's Blocks links are reconciled and both ends'
        #     status re-derived.
        #
        # Before the `field == 'status'` branch, because a person moving a
        # Backlog subtask back from Shovel Ready "keeps the status its Jira
        # status maps to" (Pass 6, decision 5.1): the re-derivation reads
        # the status the subtask held BEFORE this changelog item, which is
        # what that rule is about, and the mapped status is then recorded
        # over it.
        if item is None:
            item = mirror_subtask(project, issue, issue_key, envelope)
            work_item_id = item.id if item is not None else None
        if item is not None:
            reconcile_subtask_snapshot(project, issue, issue_key, envelope)
            item = store.get_work_item(item.id)

    if field == 'status':
        to_status = change.get('toString')
        if not to_status:
            return
        if issuetype == 'Release':
            # A Release's move to a Jira status its project's map reads as
            # `done` (Done) or `cancelled` (Abandoned): the status and its
            # release event in one block, only on a change
            # (release-mode-parity.md REQ-10, REQ-14). Any other Release
            # status falls through to the validated path below, which
            # publishes nothing.
            mapped = jira_status_to_canonical(item.project if item is not None else project, to_status)
            handler = _RELEASE_TERMINAL_HANDLERS.get(mapped)
            if handler is not None:
                handler(project, item, issue_key, envelope, to_status)
                return
        if item is None:
            _record_generic_event(project, None, issue_key, envelope,
                                   {'field': 'status', 'from': change.get('fromString'), 'to': to_status})
            return
        _apply_validated_status_change(item, to_status, issue_key, envelope)
        return

    blocked_field_id = os.environ.get('JIRA_BLOCKED_FIELD_ID')
    if blocked_field_id and change.get('fieldId') == blocked_field_id:
        _handle_blocked_field_change(project, issue, issue_key, work_item_id, change, envelope)
        return

    agent_field_id = os.environ.get('JIRA_AGENT_FIELD_ID')
    if agent_field_id and change.get('fieldId') == agent_field_id:
        _handle_agent_field_change(project, issue, issue_key, work_item_id, envelope)
        return

    # Issue links, arbitrary custom fields, anything not specifically
    # interpreted above — recorded and republished, never dropped.
    _record_generic_event(project, work_item_id, issue_key, envelope,
                           {'field': field, 'from': change.get('fromString'), 'to': change.get('toString')})


def handle_webhook_envelope(envelope: dict[str, Any], *,
                             resolve_work_item_id: Callable[[str], Any] = default_resolve_work_item_id) -> None:
    """envelope['payload']: { event, issue, body } — the same shape
    workitems/views.py's jira_webhook view (moved here from
    services/scrummaster/src/server.js per the durability amendment) publishes for every
    Jira webhook."""
    delay_ms = int(os.environ.get(_ROW_DELAY_ENV, '0') or 0)
    if delay_ms:
        import time
        time.sleep(delay_ms / 1000)

    payload = envelope['payload']
    event = payload.get('event')
    issue = payload.get('issue') or {}
    body = payload.get('body') or {}
    issue_key = issue.get('key')

    project = envelope['project']  # a required envelope field (envelope.py's own validation) — always the normalized project name the ingestion view resolved from issue.fields.project (or, for an issuelink_created, from its source issue's).

    # REQ-10: a project not in Jira mode ignores every Jira webhook —
    # recorded, not applied, and not a failure. Read once, above the
    # issue-key guard, because an `issuelink_created` body carries no
    # `issue` at all and the same ignore still applies to it (REQ-11:
    # handled "before its `issue_key` guard ... after applying the same
    # local-mode ignore to the routed project").
    in_jira_mode = project_config.get_mode(project)['mode'] == project_config.JIRA

    if event == 'issuelink_created':
        # canonical-delivery-state.md REQ-11 — Jira's own link-creation
        # event, whose body carries `issueLink` and no `issue`.
        if not in_jira_mode:
            _record_generic_event(project, None, None, envelope,
                                   {'event': event, 'ignored': 'project not in Jira mode'})
            return
        handle_issue_link_created(project, body, envelope)
        return

    if not issue_key:
        return  # not a shape this consumer understands — ignore, not a failure.

    if not in_jira_mode:
        _record_generic_event(project, None, issue_key, envelope, {'event': event, 'ignored': 'project not in Jira mode'})
        return

    if event == 'jira:issue_created':
        _handle_issue_created(project, issue, issue_key, envelope)
        return

    if event in ('comment_created', 'comment_updated'):
        _handle_comment_event(project, issue_key, body, resolve_work_item_id, envelope)
        return

    if event == 'jira:issue_updated':
        work_item_id = resolve_work_item_id(issue_key)
        changelog = ((body.get('changelog') or {}).get('items')) or []
        for change in changelog:
            _handle_changelog_item(project, issue, issue_key, work_item_id, change, envelope)
        return

    # An event kind this module has no specific interpretation for —
    # durably recorded, never silently dropped.
    work_item_id = resolve_work_item_id(issue_key)
    _record_generic_event(project, work_item_id, issue_key, envelope, {'event': event})


def create_webhook_consumer(redis_factory, project: str, *, consumer_name: str | None = None,
                             resolve_work_item_id: Callable[[str], Any] = default_resolve_work_item_id):
    def handler(envelope: dict[str, Any]) -> None:
        handle_webhook_envelope(envelope, resolve_work_item_id=resolve_work_item_id)

    # STREAM_RECLAIM_INTERVAL_MS / STREAM_RETRY_DELAY_MS: test-only
    # overrides of streams.py's production-tuned defaults (60s / 10min) —
    # a kill-mid-batch test needs a dead consumer's pending entries
    # reclaimed in seconds, not minutes. Unset in production, where the
    # longer defaults intentionally avoid reclaiming a message that's
    # simply taking a while to process.
    kwargs: dict[str, Any] = {}
    reclaim_interval_ms = os.environ.get('STREAM_RECLAIM_INTERVAL_MS')
    if reclaim_interval_ms:
        kwargs['reclaim_interval_ms'] = int(reclaim_interval_ms)
    retry_delay_ms = os.environ.get('STREAM_RETRY_DELAY_MS')
    if retry_delay_ms:
        kwargs['retry_delay_ms'] = int(retry_delay_ms)

    return create_consumer(
        redis_factory,
        stream=registry.webhook_stream_name(project),
        group=WEBHOOK_GROUP,
        consumer_name=consumer_name or os.uname().nodename,
        handler=handler,
        **kwargs,
    )
