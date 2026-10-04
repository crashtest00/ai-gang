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
    records and republishes the existing `work_item.jira_release_event`
    (kind `requested`) unchanged — see the module docstring section below
    on the Release scope carve-out, which this does not reopen.
  - `comment_created`/`comment_updated`: projected into the canonical
    comment thread via `store.append_comment` (previously
    silently discarded).
  - `jira:issue_updated` changelog, per item:
      - for a Release ticket already materialized into a `release` work
        item: `work_item_release_detail` is re-synced from the webhook's
        current `issue.fields` snapshot first (`_sync_release_detail`,
        mirroring `_sync_story_detail`) — this is how the release-candidate
        Jenkins job's writeback of Candidate SHA/Build Identifier/Preview
        URL onto the Jira ticket (`scripts/create-release-fields.sh`)
        reaches the canonical columns, regardless of which changelog field
        the webhook names.
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
      - `field == 'resolution'` to `Abandoned` for a Release: a
        `work_item.jira_release_event` (kind `abandoned`).
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

from . import jira_interpret, jira_writer, project_config, registry, status_vocabulary, store, write_gate
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
        payload={'kind': kind, 'jiraIssueKey': jira_issue_key, 'detail': detail},
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


def _sync_release_detail(item: WorkItem, fields: dict) -> None:
    """Re-parse the release fields from a webhook's current `issue`
    snapshot and persist them — mirrors `_sync_story_detail`. No-op for a
    non-release item. Target Project is deliberately not written here: it
    maps onto `item.project` (set once, at materialization time — REQ-01),
    not a `work_item_release_detail` column."""
    if item.type != 'release':
        return
    detail = jira_interpret.parse_release_fields(fields)
    WorkItemReleaseDetail.objects.update_or_create(
        work_item_id=item.id,
        defaults={
            'release_notes': detail.get('releaseNotes'),
            'candidate_sha': detail.get('candidateSha'),
            'build_identifier': detail.get('buildIdentifier'),
            'preview_url': detail.get('previewUrl'),
        },
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

# The three canonical statuses Jira shows as one Blocked flag. The writer
# pushes each of them as `set_blocked_field(true)` (REQ-09, "A status write
# to `needs-clarification`, `failed` or `cancelled`"), so the flag coming
# back is read as `needs-clarification` — which is why `failed` and
# `cancelled` are recorded as `needs-clarification` in Jira mode (§4).
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


def _materialize_release(project: str, issue: dict, issue_key: str, envelope: dict) -> Optional[WorkItem]:
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
    as `_publish_side_effect`."""
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
            return None
        _sync_release_detail(item, fields)
        return item

    # REQ-10: a Jira Release is never filed under a Target Project not in
    # Jira mode.
    if project_config.get_mode(target_project)['mode'] != project_config.JIRA:
        record_failure(target_project, None, issue_key, 'release target project not in Jira mode', fields)
        return None

    item = store.create_work_item(
        {
            'id': uuid.uuid4(), 'project': target_project, 'type': 'release',
            'displayName': display_name, 'status': 'proposed', 'externalKey': issue_key,
        },
        actor=f'jira-webhook:{issue_key}', origin=write_gate.Origins.JIRA_WEBHOOK,
    )
    _sync_release_detail(item, fields)
    return item


def _handle_release_requested(project: str, issue: dict, issue_key: str, envelope: dict) -> None:
    """A Release ticket's `jira:issue_created` creates the canonical
    `release` work item and its `work_item_release_detail` row via
    `_materialize_release` (BF-01), then republishes the existing
    `work_item.jira_release_event` (kind `requested`, `work_item_id=None`)
    unchanged. Nothing acts on that event until v5.2's outbound writer:
    ScrumMaster's former Jira-mode Release branch, which used to read
    Target Project/Candidate SHA off a fresh `jira.getIssue(issueKey)` call
    and run the beta-queue check and Jenkins trigger itself, is deleted
    (REQ-04), so a Jira-mode release event is logged as unresolved and
    triggers no Jenkins job (see module docstring). `_materialize_release`
    returns `None`, and this function publishes nothing, when REQ-10's mode
    check declines to create or re-resolve the row — a Release filed under
    a Target Project not in Jira mode, or one that resolves to a work item
    whose own project isn't."""
    with transaction.atomic():
        item = _materialize_release(project, issue, issue_key, envelope)
        if item is None:
            return

        store.write_outbox_event(
            project=project, event_type='work_item.jira_release_event', work_item_id=None,
            payload={'kind': 'requested', 'jiraIssueKey': issue_key},
        )


def _handle_release_done(project: str, issue_key: str, envelope: dict) -> None:
    with transaction.atomic():
        store.write_outbox_event(
            project=project, event_type='work_item.jira_release_event', work_item_id=None,
            payload={'kind': 'done', 'jiraIssueKey': issue_key},
        )


def _handle_release_abandoned(project: str, issue_key: str, envelope: dict) -> None:
    with transaction.atomic():
        store.write_outbox_event(
            project=project, event_type='work_item.jira_release_event', work_item_id=None,
            payload={'kind': 'abandoned', 'jiraIssueKey': issue_key},
        )


# ---------------------------------------------------------------------------
# Top-level dispatch
# ---------------------------------------------------------------------------

def _handle_issue_created(project: str, issue: dict, issue_key: str, envelope: dict) -> None:
    issuetype = _issuetype_of(issue)
    if issuetype == 'Story':
        _handle_story_created(project, issue, issue_key, envelope)
    elif issuetype == 'Release':
        _handle_release_requested(project, issue, issue_key, envelope)
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
        # helper `_handle_release_requested` uses for the create path.
        with transaction.atomic():
            item = _materialize_release(project, issue, issue_key, envelope)
        work_item_id = item.id if item is not None else None
    elif issuetype == 'Release' and item is not None and item.type == 'release':
        # The webhook's `issue` snapshot always carries the ticket's FULL
        # current field values, not just the one `change` names — re-sync
        # once per changelog item (idempotent; same value if nothing
        # release-related changed) so a Candidate SHA/Build
        # Identifier/Preview URL writeback lands regardless of which
        # changelog field the webhook happens to report it under.
        with transaction.atomic():
            _sync_release_detail(item, issue.get('fields') or {})

    if field == 'status':
        to_status = change.get('toString')
        if not to_status:
            return
        if issuetype == 'Release' and to_status == 'Done':
            _handle_release_done(project, issue_key, envelope)
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

    if field == 'resolution' and change.get('toString') == 'Abandoned' and issuetype == 'Release':
        _handle_release_abandoned(project, issue_key, envelope)
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
    if not issue_key:
        return  # not a shape this consumer understands — ignore, not a failure.

    project = envelope['project']  # a required envelope field (envelope.py's own validation) — always the normalized project name the ingestion view resolved from issue.fields.project.

    # REQ-10: a project not in Jira mode ignores every Jira webhook —
    # recorded, not applied, and not a failure.
    if project_config.get_mode(project)['mode'] != project_config.JIRA:
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
