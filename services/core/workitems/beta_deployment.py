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
from . import redis_client, registry, store
from .models import BetaDeploymentRecord
from .streams import publish

logger = logging.getLogger(__name__)

PIPELINE_FAILURE_AUTHOR = 'jenkins'


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


def apply_beta_deployment_to_work_item(work_item_id, payload: dict[str, Any], *,
                                        completion_key: str) -> None:
    """REQ-05's evidence comment and REQ-04's `in-review` transition for one
    resolved work item, evidence first — the ordering `Jenkinsfile.template`
    guaranteed for free with one sequential shell loop and that two
    independently consumed events would lose.

    Deliberately left unimplemented by this track: REQ-04 and REQ-05 own
    the body (the already-`in-review` case, the `done`/`cancelled`/`failed`
    and `ValidationError` cases, and the single webhook failure each
    records). REQ-01 owns the dispatch branch above, the resolution, and the
    deduplication record, which is what this module builds. Until that body
    lands a resolved deployment is resolved and recorded and leaves the work
    item alone — the v5.1 behaviour, not a regression."""
    logger.info(
        '[beta-deployment] resolved work item %s for build %s (completion key %s) — '
        'its evidence comment and in-review transition are REQ-04/REQ-05\'s',
        work_item_id, payload.get('build_identifier'), completion_key,
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
