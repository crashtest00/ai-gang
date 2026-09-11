"""
internal-work-item-service.md REQ-03 — the Streams command channel: the
only way an agent or ScrumMaster automation writes to canonical state.
"This service exposes no synchronous write endpoint to agents or other
system components; a create/assign/transition/attach-artifact/
append-history request arrives only as a durably queued Streams command,
following the same ack/retry/dead-letter delivery pattern redis-streams.md
already establishes." Direct port of the Node service's
src/commandConsumer.js, built on this service's own streams.py
reimplementation (see that module's comment for why it is a
reimplementation rather than a cross-language require()).
"""

from __future__ import annotations

import os
from typing import Any

from . import catchup, store, write_gate
from .materialize import materialize_decomposition
from .stream_topology import COMMAND_GROUP, command_stream_name
from .streams import PermanentError, create_consumer

PERMANENT_REJECTION_CODES = {
    'VALIDATION_ERROR', 'ASSIGNMENT_REJECTED', 'DEPENDENCY_GATE_REJECTED', 'WRITE_GATE_REJECTED',
    'MATERIALIZATION_VALIDATION_ERROR', 'MATERIALIZATION_NO_PROGRESS', 'RELEASE_GATE_REJECTED',
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
    appendComment, createLink, materializeDecomposition, recordExternalKey.
    Origin is always DIRECT here — this consumer IS the "direct" internal-API
    write path REQ-10 gates; a Jira-originated write instead goes through
    webhook_consumer.py with origin JIRA_WEBHOOK. recordExternalKey is not
    REQ-10-gated (it never touches status/assignment/dependency fields)."""
    payload = envelope['payload']
    command = payload.get('command')
    actor = payload.get('actor')

    if command == 'create':
        return store.create_work_item(payload['input'], actor=actor, origin=write_gate.Origins.DIRECT)
    if command == 'assign':
        return store.assign_work_item(payload['workItemId'], payload['agentId'], actor=actor, origin=write_gate.Origins.DIRECT)
    if command == 'transitionStatus':
        return store.transition_status(payload['workItemId'], payload['status'], actor=actor, origin=write_gate.Origins.DIRECT)
    if command == 'attachArtifact':
        return store.attach_artifact(payload['workItemId'], payload['artifactType'], payload['reference'], actor=actor)
    if command == 'appendComment':
        return store.append_comment(
            payload['workItemId'], payload['author'], payload['body'],
            reference_file=payload.get('referenceFile'), reference_function=payload.get('referenceFunction'),
            source_message_id=payload.get('sourceMessageId'),
        )
    if command == 'createLink':
        return store.create_link(payload['fromWorkItemId'], payload['toWorkItemId'], payload['linkType'],
                                  actor=actor, origin=write_gate.Origins.DIRECT)
    if command == 'recordExternalKey':
        # canonical-work-model.md REQ-15's catch-up push: the Jira-facing
        # consumer (jiraCatchupConsumer.js) reports a newly-created Jira
        # issue's key back here after catchup.py's
        # work_item.jira_catchup_requested event. Idempotent — a no-op if
        # external_key is already set, per record_external_key's own
        # contract.
        return catchup.record_external_key(payload['workItemId'], payload['externalKey'], actor=actor or 'jira-catchup')
    if command == 'materializeDecomposition':
        # canonical-work-model.md's local-mode decomposition materialization
        # / dependency-handling.md's contract, ported — see materialize.py.
        # payload['message'] is the same { parentWorkItemId, subtasks }
        # shape dependencies.js's Jira-mode function already uses.
        return materialize_decomposition(payload['message'], envelope['project'], actor=actor or 'refinement-agent')

    err = ValueError(f'unknown command "{command}"')
    err.permanent = True  # not retryable — a malformed/unsupported command will never succeed.
    raise err


def _handler(envelope: dict[str, Any]) -> None:
    try:
        handle_command(envelope)
    except Exception as err:
        if is_permanent_rejection(err):
            err.permanent = True
        raise


def create_command_consumer(redis_factory, project: str, *, consumer_name: str | None = None):
    return create_consumer(
        redis_factory,
        stream=command_stream_name(project),
        group=COMMAND_GROUP,
        consumer_name=consumer_name or os.uname().nodename,
        handler=_handler,
    )
