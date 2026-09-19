"""
Stream naming for this service — a direct port of the Node service's
src/streamTopology.js, following the same `aigang:...:{project}` topology
the Streams layer/registry.js already establish, reusing the existing
Streams topology.

Command channel (writes in): aigang:workitems:{project}, one shared
  consumer group "workitemservice".
Event stream (writes out): aigang:workitems:{project}:events
  — no single fixed group; every interested subscriber creates its own
  group when it starts consuming, so any interested subscriber can read it.
Webhook ingestion: reuses scrummaster's own aigang:webhooks:{project}
  stream (registry.webhook_stream_name) rather than a second stream — see
  webhook_consumer.py's module comment for why.
"""

COMMAND_GROUP = 'workitemservice'


def normalize_project_name(name: str) -> str:
    return str(name).strip().lower()


def command_stream_name(project: str) -> str:
    return f'aigang:workitems:{normalize_project_name(project)}'


def event_stream_name(project: str) -> str:
    return f'aigang:workitems:{normalize_project_name(project)}:events'
