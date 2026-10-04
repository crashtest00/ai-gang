"""
The Streams command channel: the
only way an agent or ScrumMaster automation writes to canonical state.
This service exposes no synchronous write endpoint to agents or other
system components; a create/assign/transition/attach-artifact/
append-history request arrives only as a durably queued Streams command,
following the same ack/retry/dead-letter delivery pattern the Streams
layer already establishes. Direct port of the Node service's
src/commandConsumer.js, built on this service's own streams.py
reimplementation (see that module's comment for why it is a
reimplementation rather than a cross-language require()).
"""

from __future__ import annotations

import logging
import os
from typing import Any

from . import beta_deployment, store, write_gate
from .materialize import materialize_decomposition
from .stream_topology import COMMAND_GROUP, command_stream_name
from .streams import create_consumer

logger = logging.getLogger(__name__)

PERMANENT_REJECTION_CODES = {
    'VALIDATION_ERROR', 'ASSIGNMENT_REJECTED', 'DEPENDENCY_GATE_REJECTED', 'WRITE_GATE_REJECTED',
    'MATERIALIZATION_VALIDATION_ERROR', 'MATERIALIZATION_NO_PROGRESS', 'RELEASE_GATE_REJECTED',
    # work-items.md REQ-04 — an artifact id that does not resolve is a
    # well-formed rejection (bad input), not a transient failure.
    'UNRESOLVED_ARTIFACT',
}


def is_permanent_rejection(err: Exception) -> bool:
    """A validation/gate/assignment rejection is a well-formed, expected
    outcome (bad input or a project in Jira mode), not a transient failure
    — dead-letter it immediately rather than burning retry attempts on
    something a retry can never fix."""
    return getattr(err, 'code', None) in PERMANENT_REJECTION_CODES


def handle_command(envelope: dict[str, Any]) -> Any:
    """envelope['payload'] shape: { command, actor, ...commandArgs }.
    `command` is one of: create, assign, transitionStatus, attachArtifact,
    appendComment, createLink, materializeDecomposition,
    recordSpecificationLink, addArtifactLink (work-items.md REQ-01/REQ-02 —
    the only write path for these two references; see that spec's REQ-03),
    and from v5.2 recordBetaDeployment and recordPipelineFailure, the two
    commands the gateway relays Jenkins' own payloads to
    (canonical-delivery-state.md REQ-01).

    Origin is always DIRECT here — this consumer IS the "direct" internal-API
    write path the router routes; a Jira-originated write instead goes through
    webhook_consumer.py with origin JIRA_WEBHOOK. recordSpecificationLink and
    addArtifactLink are not routed (neither touches a
    status/assignment/dependency field, and Jira carries no field for
    either).

    **The envelope's `messageId` is threaded to every routed handler but
    `append_comment`** (REQ-09, step 5): it is the `completion_key` the
    writer records each pushed step against, so a redelivered command whose
    Jira write already succeeded makes no second write. `append_comment`
    takes no separate key — its existing `source_message_id` IS the key, so
    this passes the payload's `sourceMessageId` where it has one and the
    envelope's `messageId` where it does not."""
    payload = envelope['payload']
    command = payload.get('command')
    actor = payload.get('actor')
    message_id = envelope.get('messageId')

    if command == 'create':
        return store.create_work_item(payload['input'], actor=actor, origin=write_gate.Origins.DIRECT)
    if command == 'assign':
        return store.assign_work_item(payload['workItemId'], payload['agentId'], actor=actor,
                                       origin=write_gate.Origins.DIRECT, completion_key=message_id)
    if command == 'transitionStatus':
        return store.transition_status(payload['workItemId'], payload['status'], actor=actor,
                                        origin=write_gate.Origins.DIRECT, completion_key=message_id)
    if command == 'attachArtifact':
        return store.attach_artifact(payload['workItemId'], payload['artifactType'], payload['reference'], actor=actor)
    if command == 'appendComment':
        return store.append_comment(
            payload['workItemId'], payload['author'], payload['body'],
            reference_file=payload.get('referenceFile'), reference_function=payload.get('referenceFunction'),
            source_message_id=payload.get('sourceMessageId') or message_id,
            origin=write_gate.Origins.DIRECT,
        )
    if command == 'createLink':
        return store.create_link(payload['fromWorkItemId'], payload['toWorkItemId'], payload['linkType'],
                                  actor=actor, origin=write_gate.Origins.DIRECT, completion_key=message_id)
    if command == 'recordSpecificationLink':
        return store.record_specification_link(
            payload['workItemId'], payload['artifactId'], payload['requirementId'], actor=actor,
        )
    if command == 'addArtifactLink':
        return store.add_artifact_link(payload['workItemId'], payload['artifactId'], actor=actor)
    if command == 'materializeDecomposition':
        # Decomposition materialization, ported from the dependency graph's
        # contract — see materialize.py. payload['message'] is the same
        # { parentWorkItemId, subtasks } shape dependencies.js's function
        # already uses.
        # REQ-09, step 5: the envelope's `messageId` reaches every routed
        # handler but `append_comment`, and `materialize_decomposition` is
        # the fifth of them — on `push` its root transitions skip any step
        # the writer's completion record already shows done.
        return materialize_decomposition(payload['message'], envelope['project'],
                                          actor=actor or 'refinement-agent',
                                          completion_key=message_id)
    if command == 'recordBetaDeployment':
        # canonical-delivery-state.md REQ-01 — the gateway relays Jenkins'
        # `beta_deployed` payload here, carrying the gateway envelope's own
        # messageId as `sourceMessageId`, because `publishCommand` mints a
        # fresh `messageId` on each relay and REQ-01's and REQ-05's keys
        # must survive a re-relay.
        return beta_deployment.record_beta_deployment(
            payload, envelope['project'],
            source_message_id=payload.get('sourceMessageId') or message_id,
        )
    if command == 'recordPipelineFailure':
        return beta_deployment.record_pipeline_failure(
            payload, envelope['project'],
            source_message_id=payload.get('sourceMessageId') or message_id,
        )

    err = ValueError(f'unknown command "{command}"')
    err.permanent = True  # not retryable — a malformed/unsupported command will never succeed.
    raise err


# ---------------------------------------------------------------------------
# Rejections, in every mode (canonical-delivery-state.md REQ-09)
# ---------------------------------------------------------------------------

def _rejection_detail(err: Exception) -> list[str]:
    """The detail each rejection carries, as REQ-09 lists it: rejected
    subtasks and permitted agents; an assignment's permitted agents; the
    dependency gate's incomplete blockers; unresolved subtasks and their
    blockers."""
    lines: list[str] = []

    rejected = getattr(err, 'rejected', None)
    if rejected:
        # `assignment.validate_decomposition`'s own entry shape.
        lines.append('Rejected subtasks: ' + ', '.join(
            f'{r.get("subtaskId")} ("{r.get("displayName")}", requested agent "{r.get("requestedAgent")}")'
            if isinstance(r, dict) else str(r)
            for r in rejected
        ))

    permitted = getattr(err, 'permitted_agents', None)
    if permitted is None:
        result = getattr(err, 'result', None)
        if isinstance(result, dict):
            permitted = result.get('permittedAgents')
    if permitted is not None:
        lines.append('Permitted agents for this project: ' + (', '.join(permitted) or '(none configured)'))

    blockers = getattr(err, 'blockers', None)
    if blockers:
        lines.append('Blockers not yet done: ' + ', '.join(str(b) for b in blockers))

    unresolved = getattr(err, 'unresolved', None)
    if unresolved:
        lines.append('Unresolved subtasks: ' + ', '.join(
            f'{u.get("id")} (blocked by {", ".join(str(b) for b in (u.get("blockedBy") or [])) or "nothing"})'
            if isinstance(u, dict) else str(u)
            for u in unresolved
        ))

    subtask_id = getattr(err, 'subtask_id', None)
    if subtask_id is not None:
        lines.append(
            f'No subtask was created for {subtask_id}; fix the reference and retry the decomposition.'
        )

    return lines


def _rejection_work_item_id(payload: dict[str, Any]) -> Any:
    """The work item the rejection comment goes on — for a decomposition,
    the parent."""
    if payload.get('command') == 'materializeDecomposition':
        return (payload.get('message') or {}).get('parentWorkItemId')
    return payload.get('workItemId') or (payload.get('input') or {}).get('id')


def append_rejection_comment(envelope: dict[str, Any], err: Exception) -> None:
    """REQ-09, "Rejections, in every mode": a command `core` rejects for a
    validation leaves exactly ONE comment on its work item (for a
    decomposition, on the parent), in every mode, through the one comment
    path, keyed `<messageId>:rejection` so a redelivery adds none. It is
    written after the rejected write's transaction has rolled back — this
    runs in the consumer's handler, outside any of them.

    The release gate keeps today's comment, appended inside
    `transition_status` itself, and this adds no second one for it.
    A rejection whose work item (or parent) does not exist leaves no
    comment."""
    payload = envelope.get('payload') or {}
    if getattr(err, 'code', None) == 'RELEASE_GATE_REJECTED':
        return

    work_item_id = _rejection_work_item_id(payload)
    if not work_item_id:
        return
    if store.get_work_item(work_item_id) is None:
        return

    body = f'[system] {payload.get("command")} rejected: {err}'
    detail = _rejection_detail(err)
    if detail:
        body += '\n\n' + '\n'.join(detail)

    store.append_comment(
        work_item_id, 'system', body,
        source_message_id=f'{envelope.get("messageId")}:rejection' if envelope.get('messageId') else None,
        origin=write_gate.Origins.DIRECT,
    )


def _handler(envelope: dict[str, Any]) -> None:
    try:
        handle_command(envelope)
    except Exception as err:
        if is_permanent_rejection(err):
            err.permanent = True
            try:
                append_rejection_comment(envelope, err)
            except Exception as comment_err:  # noqa: BLE001 - see below
                # REQ-09: "If its post fails transiently, the handler raises
                # that failure as transient. The redelivered command is
                # rejected again and posts only the comment not yet recorded
                # complete, and is then dead-lettered." So the transient
                # failure replaces the permanent one here deliberately: a
                # dead letter written now would lose the comment for good.
                logger.error(
                    '[command] %s was rejected permanently (%r) but its rejection comment could not be '
                    'posted — raising the post failure so the command is redelivered',
                    envelope.get('messageId'), err,
                )
                raise comment_err from err
        raise


def create_command_consumer(redis_factory, project: str, *, consumer_name: str | None = None):
    return create_consumer(
        redis_factory,
        stream=command_stream_name(project),
        group=COMMAND_GROUP,
        consumer_name=consumer_name or os.uname().nodename,
        handler=_handler,
    )
