"""
canonical-work-model.md REQ-09 (durable, retried webhook ingestion), REQ-11
(Jira-originated events are validated before being applied) and REQ-22
(all platform-specific interpretation lives in Django; every ingested
event is republished).

**Amended 2026-09-09 for REQ-09/REQ-22.** This module originally only
applied a `changelog` entry with `field == 'status'` and silently ignored
everything else (comments, issue links, issue creation, Release events) —
a known, tracked gap. It now interprets the full webhook payload:

  - `jira:issue_created` for a Story: creates the canonical work item
    (REQ-17's five-field gate decides whether it starts `ready`
    (dispatch-eligible) or stuck in `proposed`), matching
    handlers.js's `handleStoryCreated`.
  - `jira:issue_created` for a Release: recorded and republished as a
    `work_item.jira_release_event` (kind `requested`) — see the module
    docstring section below on the Release scope carve-out.
  - `comment_created`/`comment_updated`: projected into REQ-18's
    canonical comment thread via `store.append_comment` (previously
    silently discarded).
  - `jira:issue_updated` changelog, per item:
      - `field == 'status'`: the existing REQ-11-validated transition
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
        event rather than dropped (REQ-22: "a consumer with no use for a
        given event today MAY ignore it, but Django MUST NOT decide on
        ingestion that an event is irrelevant and drop it").

**Scope carve-out, deliberate (see final report for the full reasoning):**
Release-ticket business logic (the beta-queue-clean check ahead of cutting
a release candidate, and the Jenkins job triggers themselves) is NOT
reimplemented here. `internal-work-item-service.md`'s own REQ-09
resolution establishes that this service "has no Jira client of its own
... and never calls Jira directly," and the beta-queue-clean check needs a
live Jira JQL search ScrumMaster's Jira-mode subtask-creation path
(`dependencies.js`'s `materializeDecomposition`, unchanged/out of scope
for this task) does not mirror into this service's own store — so this
service cannot correctly answer "is the beta queue clean" from its own
data today. This module's job for a Release event is therefore limited to
INTERPRETATION (recognizing the event and durably recording/republishing
it as a canonical `work_item.jira_release_event`); the existing, already-
tested `handleReleaseRequested`/`handleReleaseAbandoned`/`handleDone`
Release branch in `services/scrummaster/src/handlers.js` remain the executors,
now invoked by ScrumMaster's new dispatch/side-effect consumer
(`dispatchConsumer.js`) reacting to that canonical event instead of by
`server.js`'s deleted `routeWebhookEvent`.

Design decision (carried over unchanged from before this amendment):
REQ-09's durability requirement itself is satisfied by
`workitems/views.py`'s `jira_webhook` view, which now durably enqueues
every inbound Jira webhook onto `aigang:webhooks:{project}` — moved here
from `services/scrummaster/src/server.js` per REQ-09's 2026-09-09 amendment ("Django
... is AI Gang's sole external-facing surface"). This module remains the
consumer half: the `workitemservice` consumer group on that same stream.
"""

from __future__ import annotations

import os
import uuid
from typing import Any, Callable, Optional

from django.db import transaction
from django.utils import timezone

from . import jira_interpret, project_config, registry, status_vocabulary, store, write_gate
from .models import WebhookFailure, WorkItem, WorkItemStoryDetail
from .streams import create_consumer

WEBHOOK_GROUP = 'workitemservice'

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


# canonical-work-model.md REQ-02: "each canonical status MUST map to a
# configured Jira status through validated project configuration, not a
# hardcoded or display-label-dependent mapping." This is the fallback for a
# project that hasn't declared its own mapping via
# project_config.declare_custom_status — not itself the validated
# configuration REQ-02 requires.
DEFAULT_JIRA_STATUS_MAP = {
    'Shovel Ready': 'ready',
    'In Progress': 'in-progress',
    'In Review': 'in-review',
    'Done': 'done',
    'Backlog': 'proposed',
}


def jira_status_to_canonical(project: str, jira_status_name: str) -> str:
    """Per-project configured mapping first (ProjectStatusConfig rows,
    covering both minimum-set overrides and custom statuses — REQ-02 does
    not distinguish the two for mapping purposes), falling back to the
    built-in default map for a project that hasn't configured its own."""
    for row in project_config.get_custom_statuses(project):
        if row['jira_status_name'] == jira_status_name:
            return row['status']
    return DEFAULT_JIRA_STATUS_MAP.get(jira_status_name, jira_status_name)


def _publish_side_effect(project: str, work_item_id, jira_issue_key: str, kind: str, detail: dict) -> None:
    """A small, Django-decided instruction for ScrumMaster's Jira-facing
    side-effect consumer (dispatchConsumer.js) — e.g. "post this exact
    missing-fields comment," "trigger the release-candidate Jenkins job."
    The DECISION (what happened, what should happen next) is made here;
    the consumer only executes the Jira/Jenkins call using values this
    event already carries. Must be called from inside an existing
    transaction.atomic() block so it lands atomically with whatever
    canonical write (if any) it accompanies (REQ-06)."""
    store.write_outbox_event(
        project=project, event_type='work_item.jira_side_effect', work_item_id=work_item_id,
        payload={'kind': kind, 'jiraIssueKey': jira_issue_key, 'detail': detail},
    )


def _record_generic_event(project: str, work_item_id, jira_issue_key: str, envelope: dict, detail: dict) -> None:
    """REQ-22: "Django MUST durably record and republish every ingested
    event as a canonical domain event ... not only the subset that maps to
    an existing canonical work-item field." Catch-all for anything this
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
    """Re-parse the REQ-17 story fields from a webhook's current `issue`
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


def _apply_validated_status_change(item: WorkItem, jira_status_name: str, issue_key: str, envelope: dict) -> None:
    target_status = jira_status_to_canonical(item.project, jira_status_name)
    try:
        store.transition_status(item.id, target_status, actor=f'jira-webhook:{issue_key}',
                                 origin=write_gate.Origins.JIRA_WEBHOOK)
    except Exception as err:
        # REQ-11: "MUST be rejected and recorded as a failure event...
        # rather than applied... MUST NOT be treated as automatically
        # correct because it originated in Jira."
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

    with transaction.atomic():
        if WorkItem.objects.filter(external_key=issue_key).exists():
            return  # redelivery of an issue_created webhook already materialized — idempotent no-op.

        item = store.create_work_item(
            {
                'id': uuid.uuid4(), 'project': project, 'type': 'story',
                'displayName': display_name, 'description': description,
                'status': status, 'assigneeAgentId': 'refinement-agent',
                'externalKey': issue_key, 'storyDetail': detail,
            },
            actor=f'jira-webhook:{issue_key}', origin=write_gate.Origins.JIRA_WEBHOOK,
        )
        # handlers.js always sets the Agent field and posts the
        # acknowledgement comment BEFORE checking required fields — ok is
        # carried through so ScrumMaster's side-effect consumer can decide
        # which of the two (ack vs. missing-fields-block) to perform.
        _publish_side_effect(project, item.id, issue_key, 'story_intake', {'ok': not missing, 'missing': missing})


# ---------------------------------------------------------------------------
# Handler 3 (handlers.js `handleBlockedCleared`) — the JIRA_BLOCKED_FIELD_ID
# custom field going from truthy to falsy.
# ---------------------------------------------------------------------------

def _handle_blocked_field_change(project: str, issue: dict, issue_key: str, work_item_id, change: dict,
                                  envelope: dict) -> None:
    became_unblocked = bool(change.get('from')) and not change.get('to')
    if not became_unblocked:
        # V1 never had a "became blocked" webhook-driven handler either —
        # Blocked is only ever SET by ScrumMaster's own side effects (a
        # missing-fields or assignment-rejection comment), not a human
        # action this module needs to react to.
        return

    if work_item_id is None:
        _record_generic_event(project, None, issue_key, envelope, {'field': 'blocked', 'cleared': True})
        return

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
        # branch: re-run the exact same REQ-17 gate by attempting the same
        # transition a fresh Shovel-Ready-equivalent would attempt. Success
        # publishes the ordinary work_item.status_changed event, which is
        # all the dispatch consumer needs (REQ-21) — no extra event required.
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
            with transaction.atomic():
                _publish_side_effect(project, item.id, issue_key, 'story_intake', {'ok': False, 'missing': missing, 'reblock': True})
        return

    # Everything else (a dev-agent ticket blocked mid-implementation): no
    # canonical status change — clearing Blocked must not itself move the
    # ticket's status. Just tell the dispatch consumer to redispatch this
    # item as a continuation (buildUnblockPrompt + BLOCKED-marker search,
    # matching handleBlockedCleared's non-refinement branch exactly).
    with transaction.atomic():
        _publish_side_effect(project, item.id, issue_key, 'blocked_cleared', {})


# ---------------------------------------------------------------------------
# Comments (REQ-18) and Release events — see module docstring for the
# Release scope carve-out.
# ---------------------------------------------------------------------------

def _handle_comment_event(project: str, issue_key: str, body: dict, resolve_work_item_id, envelope: dict) -> None:
    comment = body.get('comment') or {}
    work_item_id = resolve_work_item_id(issue_key)
    author = (comment.get('author') or {}).get('displayName') or 'Unknown'
    text = jira_interpret.adf_to_text(comment.get('body')).strip()
    comment_id = comment.get('id')

    if work_item_id is None:
        _record_generic_event(project, None, issue_key, envelope, {'event': 'comment', 'author': author, 'body': text})
        return

    with transaction.atomic():
        store.append_comment(
            work_item_id, author, text,
            source_message_id=f'jira-comment:{issue_key}:{comment_id}' if comment_id else None,
        )


def _handle_release_requested(project: str, issue: dict, issue_key: str, envelope: dict) -> None:
    with transaction.atomic():
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

    if field == 'status':
        to_status = change.get('toString')
        if not to_status:
            return
        if issuetype == 'Release' and to_status == 'Done':
            _handle_release_done(project, issue_key, envelope)
            return
        if work_item_id is None:
            _record_generic_event(project, None, issue_key, envelope,
                                   {'field': 'status', 'from': change.get('fromString'), 'to': to_status})
            return
        item = store.get_work_item(work_item_id)
        if not item:
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
    # interpreted above — recorded and republished, never dropped (REQ-22).
    _record_generic_event(project, work_item_id, issue_key, envelope,
                           {'field': field, 'from': change.get('fromString'), 'to': change.get('toString')})


def handle_webhook_envelope(envelope: dict[str, Any], *,
                             resolve_work_item_id: Callable[[str], Any] = default_resolve_work_item_id) -> None:
    """envelope['payload']: { event, issue, body } — the same shape
    workitems/views.py's jira_webhook view (moved here from
    services/scrummaster/src/server.js per REQ-09's amendment) publishes for every
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
    # durably recorded, never silently dropped (REQ-22).
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
