"""
The A2A archive group — durable-a2a-message-record.md §4, "Where the write
happens: Core's archive group on the raw streams".

Core archives every A2A message as it appears on Redis Streams, through a
consumer group of its own, ``core-a2a-archive``, on two kinds of stream per
configured project:

  aigang:gateway:{project}           agent -> ScrumMaster submissions
  aigang:agent:{project}:{suffix}    ScrumMaster -> agent dispatches

The streams are ScrumMaster's; Core only reads them, in its own group, beside
the groups already on them (``scrummaster`` on a gateway stream, ``agent-
{suffix}`` on an agent channel stream). It enumerates them from the same
``agents.json``/``projects.json`` ScrumMaster reads (``registry.py``), once,
when the consumers start, and starts one archive consumer per distinct stream
name, since two agents may share a ``channelSuffix``.

Each entry is stored as it appeared, whether or not ScrumMaster then accepted
it: the envelope, the A2A message, the state it carried and its artifacts,
keyed by the A2A message's own ``messageId`` (``store.record_a2a_message``,
one transaction, acknowledged only after it commits). ScrumMaster's verdict on
a submission reaches Core separately, on the command stream
(``recordA2AVerdict``, ``command_consumer.py``).

**This group never dead-letters** (§4, "Core's archive group writes no dead
letter"). The ``:dead`` stream of these streams is the one ScrumMaster's
gateway consumer and the project container's subscriber also write, its
records name no group, and a replay re-adds the entry for every group on the
stream — on an agent channel stream, a second dispatch to the agent. So the
consumers here run in ``streams.Consumer``'s archive mode: an entry this
module does not store is acknowledged, and an entry whose write fails stays
pending and is retried without a limit.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Callable

from . import registry, store
from .envelope import Kind, validate_envelope
from .models import A2AMessage
from .streams import Consumer

logger = logging.getLogger(__name__)

A2A_ARCHIVE_GROUP = 'core-a2a-archive'

GATEWAY_STREAM = 'gateway'
AGENT_CHANNEL_STREAM = 'agent_channel'


def archive_targets() -> list[dict[str, str]]:
    """Every stream the archive group reads, once each: each configured
    project's gateway stream, and the agent channel stream of each agent the
    project enables (``get_project(name)['agents']``, each agent's
    ``routing.channelSuffix``) — the set ScrumMaster creates and trims
    (``index.js``'s ``bootstrapStreams`` and ``everyStreamGroup``, through
    ``getEffectiveAgents``). A stream name already listed is not listed
    again, whichever project or agent names it first."""
    registry.load()
    targets: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(stream: str, project: str, kind: str) -> None:
        if stream in seen:
            return
        seen.add(stream)
        targets.append({'stream': stream, 'project': project, 'kind': kind})

    for project in registry.get_project_names():
        add(registry.gateway_stream_name(project), project, GATEWAY_STREAM)
        for agent_id in registry.get_project(project)['agents']:
            suffix = registry.get_agent(agent_id)['routing']['channelSuffix']
            add(registry.agent_stream_name(project, suffix), project, AGENT_CHANNEL_STREAM)
    return targets


def _is_non_a2a_gateway_branch(envelope: dict[str, Any]) -> bool:
    """The four branches ``dispatchGatewayOperation``
    (``services/scrummaster/src/gateway.js``) routes away from
    ``handleA2ASubmission``, tested the same way and in the same order (§4,
    "v5.2's gateway relay branch is not A2A content")."""
    if envelope.get('kind') == Kind.TASK_STATUS:
        return True
    payload = envelope.get('payload') or {}
    if payload.get('operation') == 'materializeDecomposition':
        return True
    if payload.get('type') == 'beta_deployed':
        return True
    if payload.get('type') == 'pipeline_retry':
        return True
    return False


def archive_entry(target: dict[str, str], entry_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    """The handler for one entry of ``target['stream']``. Returns what it did,
    for logging and tests; returning at all is what acknowledges the entry.

    Not stored, and acknowledged: an entry that is not a valid envelope, one
    that carries no ``taskId`` or no A2A message ``messageId``, and, on a
    gateway stream, every non-A2A branch. Everything else is stored, however
    malformed the rest of it is (§4, "What Core stores")."""
    raw = fields.get('data') if isinstance(fields, dict) else None
    try:
        envelope = json.loads(raw) if isinstance(raw, str) else None
        validate_envelope(envelope)
    except Exception:  # noqa: BLE001 - any parse or validation failure means "cannot key it"
        return {'stored': False, 'skipped': 'not a valid envelope'}

    if target['kind'] == GATEWAY_STREAM:
        if _is_non_a2a_gateway_branch(envelope):
            return {'stored': False, 'skipped': 'not an A2A submission'}
        message = envelope['payload'].get('message')
        direction = A2AMessage.SUBMISSION
    else:
        # dispatchTask is the only publisher here, with the A2A message as the
        # whole payload (handlers.js).
        message = envelope['payload']
        direction = A2AMessage.DISPATCH

    task_id = envelope.get('taskId')
    message_id = message.get('messageId') if isinstance(message, dict) else None
    if not (isinstance(task_id, str) and task_id) or not (isinstance(message_id, str) and message_id):
        return {'stored': False, 'skipped': 'no taskId or A2A messageId to key it by'}

    return store.record_a2a_message(
        outbox_project=target['project'], stream=target['stream'], entry_id=entry_id, direction=direction,
        envelope=envelope, task_id=task_id, message_id=message_id,
    )


def create_archive_consumers(redis_factory: Callable[[], Any], *, consumer_name: str | None = None,
                             **consumer_options) -> list[Consumer]:
    """One archive-mode consumer per stream ``archive_targets`` lists, not yet
    started. ``consumer_options`` passes timing overrides through, for
    tests."""
    consumers = []
    for target in archive_targets():
        def handler(entry_id, fields, target=target):
            result = archive_entry(target, entry_id, fields)
            if result.get('skipped'):
                logger.info('[a2a-archive] %s entry %s not stored: %s', target['stream'], entry_id, result['skipped'])
            return result

        consumers.append(Consumer(
            redis_factory,
            stream=target['stream'],
            group=A2A_ARCHIVE_GROUP,
            consumer_name=consumer_name or os.uname().nodename,
            handler=handler,
            archive=True,
            **consumer_options,
        ))
    return consumers
