"""
The two commands the gateway relays Jenkins' own payloads to
(canonical-delivery-state.md REQ-01).

Jenkins publishes a `beta_deployed` payload on a successful post-merge dev
build and a `pipeline_retry` payload on a failed build, both onto the
project's gateway stream through the raw entry point of the commons publish
tool. `gateway.js`'s `dispatchGatewayOperation` relays each to `core` with
`publishCommand` as `recordBetaDeployment` and `recordPipelineFailure`, and
`command_consumer.handle_command` dispatches them here.

Neither payload names work by tracker key: Jenkins carries the promoted
**pull requests** (a promoted commit with no merged PR is carried as its
SHA), and `core` resolves each reference to the work item that recorded it
through REQ-02's resolver. A reference that resolves to nothing is logged at
error level as unresolved and dispatches nobody.

The relay carries the GATEWAY envelope's `messageId` as `sourceMessageId`,
not the command envelope's own `messageId`, which `publishCommand` mints
afresh on each relay — so REQ-01's and REQ-05's keys survive a re-relay of
the same Jenkins message.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from django.db import IntegrityError, transaction
from django.utils import timezone

from . import envelope as envelope_module
from . import jira_writer, project_config, redis_client, registry, status_vocabulary, store, write_gate
from .models import BetaDeploymentRecord
from .streams import publish

logger = logging.getLogger(__name__)

PIPELINE_FAILURE_AUTHOR = 'jenkins'

# REQ-05's evidence comment and REQ-01's failure comment share this literal
# author — both are canonical writes `core` makes on Jenkins' behalf, not
# Jenkins calling out itself.
BETA_EVIDENCE_AUTHOR = 'jenkins'

# REQ-09's step name for a status write, reused here rather than invented
# again: the webhook failure this module records for a no-legal-transition
# or `ValidationError` outcome is, like the writer's own, a failure of the
# "status" step.
STEP_STATUS = jira_writer.STEP_STATUS


def _references(payload: dict[str, Any]) -> list[str]:
    """The promoted pull requests, or a SHA for a promoted commit with no
    merged pull request (or whose lookup failed) — REQ-01's one list, under
    whichever of the two names the producer used."""
    raw = payload.get('pull_requests')
    if raw is None:
        raw = payload.get('references') or []
    if isinstance(raw, str):
        raw = [part for part in raw.split() if part]
    return [str(reference).strip() for reference in raw if str(reference).strip()]


def resolve_references(payload: dict[str, Any], *, message: str) -> list[str]:
    """Each reference through REQ-02's resolver, de-duplicated across
    references (two promoted pull requests can belong to one work item), in
    the order the references arrived."""
    work_item_ids: list[str] = []
    for reference in _references(payload):
        for work_item_id in store.resolve_work_items_for_reference(reference, message=message):
            if work_item_id not in work_item_ids:
                work_item_ids.append(work_item_id)
    return work_item_ids


# ---------------------------------------------------------------------------
# recordBetaDeployment (REQ-01; its per-item evidence and transition are
# REQ-04/REQ-05's, built on the seam below)
# ---------------------------------------------------------------------------

def already_recorded(payload: dict[str, Any]) -> bool:
    """REQ-01 — `core` deduplicates a `beta_deployed` event on the deployed
    SHA and the build identifier, because the gateway deduplicates only by
    `messageId`: "a second message for the same SHA and build is
    acknowledged with no write"."""
    return BetaDeploymentRecord.objects.filter(
        deployed_sha=payload.get('deployed_sha') or '',
        build_identifier=payload.get('build_identifier') or '',
    ).exists()


def record_beta_deployment(payload: dict[str, Any], project: str, *,
                            source_message_id: Optional[str] = None) -> dict[str, Any]:
    """Resolve the promoted pull requests, apply the deployment to each
    work item it resolves, and record the deployment — in that order, the
    record written only once the last step has completed, so a redelivery
    of the same message runs its own incomplete steps (REQ-09
    "Redelivery") while a second message for the same deployment writes
    nothing.

    `apply_beta_deployment_to_work_item` is the per-item step:
    REQ-05's evidence comment and then REQ-04's transition, in that order,
    each keyed `<sourceMessageId>:<workItemId>`."""
    if already_recorded(payload):
        logger.info(
            '[beta-deployment] %s / build %s is already recorded — acknowledged with no write',
            payload.get('deployed_sha'), payload.get('build_identifier'),
        )
        return {'deduped': True, 'workItemIds': []}

    message = f'beta_deployed {source_message_id or "(no source messageId)"}'
    work_item_ids = resolve_references(payload, message=message)

    for work_item_id in work_item_ids:
        apply_beta_deployment_to_work_item(
            work_item_id, payload, completion_key=f'{source_message_id}:{work_item_id}',
        )

    try:
        with transaction.atomic():
            BetaDeploymentRecord.objects.create(
                project=registry.normalize_project_name(project),
                deployed_sha=payload.get('deployed_sha') or '',
                build_identifier=payload.get('build_identifier') or '',
                build_url=payload.get('build_url'),
                beta_url=payload.get('beta_url'),
                source_message_id=source_message_id,
                work_item_ids=work_item_ids,
                recorded_at=timezone.now(),
            )
    except IntegrityError:
        # `uq_beta_deployment_sha_build` — another delivery of the same
        # deployment recorded it between the check above and here. The
        # record says the deployment is applied; which message applied it
        # does not matter.
        logger.info('[beta-deployment] %s / build %s was recorded concurrently',
                    payload.get('deployed_sha'), payload.get('build_identifier'))
        return {'deduped': True, 'workItemIds': work_item_ids}

    return {'deduped': False, 'workItemIds': work_item_ids}


def evidence_comment_body(payload: dict[str, Any]) -> str:
    """REQ-05's beta-evidence comment: "the comment carries the Beta URL,
    deployed SHA, build identifier and build log" — the same content
    `Jenkinsfile.template:254-278` posted directly to Jira before this
    stage, now built here instead of by Jenkins."""
    lines = ['Deployed to beta.']
    beta_url = payload.get('beta_url')
    if beta_url:
        lines.append(f'Beta: {beta_url}')
    lines.append(f'Deployed SHA: {payload.get("deployed_sha") or "(unknown)"}')
    lines.append(f'Build: {payload.get("build_identifier") or "(unknown)"}')
    build_url = payload.get('build_url')
    if build_url:
        lines.append(f'Build log: {build_url}')
    return '\n'.join(lines)


def apply_beta_deployment_to_work_item(work_item_id, payload: dict[str, Any], *,
                                        completion_key: str) -> None:
    """REQ-05's evidence comment and REQ-04's `in-review` transition for one
    resolved work item, evidence first — the ordering `Jenkinsfile.template`
    guaranteed for free with one sequential shell loop and that two
    independently consumed events would lose. Both calls carry the same
    `completion_key` (`<sourceMessageId>:<workItemId>`): the comment path's
    own step (`comment`) and `transition_status`'s push step (`status`)
    never collide on it, and a redelivery that already completed one of the
    two steps skips only that one.

    The handler states no mode: `store.append_comment` and
    `store.transition_status` each ask `write_gate.route` and either record
    or push accordingly (REQ-09). In Jira mode the comment call pushes
    in-process before this returns, so the transition call that follows
    cannot reach Jira first (REQ-05, "The order holds because one handler
    makes both calls, comment first").

    REQ-04's no-legal-transition handling reads the work item's *canonical
    baseline status* (a custom status resolves to its baseline first,
    `status_vocabulary.baseline_of`), not the project's mode:

      already `in-review`  the write already took effect — do nothing
                            further, record no webhook failure;
      `done`/`cancelled`/`failed`  no legal transition — the evidence
                            comment above still lands, `transition_status`
                            is not called, and exactly one webhook failure
                            is recorded naming this work item and the
                            `status` step;
      anything else         call `transition_status`; a `ValidationError`
                            from it is treated the same as a no-legal-
                            transition outcome (comment kept, one webhook
                            failure recorded, nothing raised to the
                            consumer).

    A Jira-mode push's OWN missing-transition outcome (the ticket offers no
    In Review transition) is `jira_writer`'s to record, inside
    `transition_status`'s push branch — not duplicated here."""
    item = store.get_work_item(work_item_id)
    if item is None:
        # The association that resolved this id still pointed somewhere;
        # the work item itself is gone by the time delivery caught up. Not
        # one of REQ-04's named outcomes — logged, not recorded as a
        # webhook failure naming a work item that no longer exists.
        logger.error(
            '[beta-deployment] work item %s resolved for build %s no longer exists — '
            'no evidence comment, no transition',
            work_item_id, payload.get('build_identifier'),
        )
        return

    store.append_comment(
        work_item_id, BETA_EVIDENCE_AUTHOR, evidence_comment_body(payload),
        source_message_id=completion_key, origin=write_gate.Origins.DIRECT,
    )

    custom_statuses = project_config.get_custom_statuses(item.project)
    baseline = status_vocabulary.baseline_of(item.status, custom_statuses)

    if baseline == 'in-review':
        return

    if baseline in status_vocabulary.TERMINAL_STATUSES:
        jira_writer.record_webhook_failure(
            item,
            f'beta deployment: work item {work_item_id} has no legal transition to "in-review" '
            f'(status is "{item.status}")',
            {'step': STEP_STATUS, 'build_identifier': payload.get('build_identifier')},
        )
        return

    try:
        store.transition_status(
            work_item_id, 'in-review', actor=BETA_EVIDENCE_AUTHOR,
            origin=write_gate.Origins.DIRECT, completion_key=completion_key,
        )
    except store.ValidationError as err:
        jira_writer.record_webhook_failure(
            item,
            f'beta deployment: transitionStatus raised a ValidationError for work item '
            f'{work_item_id}: {err}',
            {'step': STEP_STATUS, 'build_identifier': payload.get('build_identifier')},
        )


# ---------------------------------------------------------------------------
# recordPipelineFailure (REQ-01, "A failed build reports the same pull
# requests, and `core` resolves them")
# ---------------------------------------------------------------------------

def failure_comment_body(payload: dict[str, Any]) -> str:
    """The same comment Jenkins posted to each affected ticket before it
    published the retry (`Jenkinsfile.template`'s failure handler), which
    `canonical-work-model.md` REQ-12 forbids dropping. Jenkins still builds
    the text; `core` appends it."""
    text = payload.get('failure_text') or 'The project pipeline failed.'
    build_url = payload.get('build_url')
    body = text
    if build_url:
        body += f'\n\nBuild log: {build_url}'
    body += '\n\nPlease review and fix.'
    return body


def record_pipeline_failure(payload: dict[str, Any], project: str, *,
                             source_message_id: Optional[str] = None) -> dict[str, Any]:
    """Resolve the failed build's pull requests, append the failure comment
    to each work item resolved, and publish the retry for ScrumMaster with
    the canonical `workItemId`. ScrumMaster's own retry handler then
    redispatches that work item's recorded owner.

    `core` publishes one retry per resolved work item, onto the project's
    gateway stream (`aigang:gateway:<project>`, kind `gateway_operation`).
    ScrumMaster tells `core`'s retry from Jenkins' by that canonical
    `workItemId`, which only `core`'s carries."""
    message = f'pipeline_retry {source_message_id or "(no source messageId)"}'
    work_item_ids = resolve_references(payload, message=message)

    body = failure_comment_body(payload)
    client = None
    stream = registry.gateway_stream_name(project)

    for work_item_id in work_item_ids:
        # Through `core`'s one comment path, author `jenkins`, keyed
        # `<sourceMessageId>:<workItemId>` so a redelivery — or a re-relay
        # of the same Jenkins message — adds none, and N items get N
        # comments.
        store.append_comment(
            work_item_id, PIPELINE_FAILURE_AUTHOR, body,
            source_message_id=f'{source_message_id}:{work_item_id}' if source_message_id else None,
        )

        if client is None:
            client = redis_client.new_client()
        retry_envelope = envelope_module.build_envelope(
            envelope_module.Kind.GATEWAY_OPERATION,
            registry.normalize_project_name(project),
            payload={
                'type': 'pipeline_retry',
                'workItemId': str(work_item_id),
                'build_url': payload.get('build_url'),
                'build_number': payload.get('build_number'),
            },
        )
        publish(
            client, stream, retry_envelope,
            # The retry for one (work item, source message) pair is one
            # logical message: a redelivered command republishes nothing,
            # and ScrumMaster's own per-(work item, build) dedupe is the
            # second line rather than the only one.
            dedupe_key=f'pipeline-retry:{source_message_id}:{work_item_id}' if source_message_id else None,
        )

    return {'workItemIds': work_item_ids}
