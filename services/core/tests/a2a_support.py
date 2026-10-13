"""
Shared harness for the durable A2A record's tests (durable-a2a-message-record.md).

Not collected by pytest (``python_files = test_*.py``). It gives each test the
three real parties the record is built from, against the real test Postgres
and Redis:

- **core's consumers** — the ``core-a2a-archive`` consumers
  (``a2a_archive.create_archive_consumers``, archive mode) and the project's
  command consumer (``command_consumer._handler``, the handler
  ``create_command_consumer`` installs), started on background threads with
  test timing (``CoreConsumers``);
- **ScrumMaster** — its own modules, run in a Node child process by
  ``a2a_scrummaster_driver.js`` (``ScrumMaster``): ``handlers.dispatchTask``
  for every dispatch, the gateway's consumer wiring for every verdict;
- **an agent** — the real submit tool, ``setup/commons/tools/a2a-submit.js``,
  run as a child process (``submit_tool``), or the raw entry point
  ``gateway-publish.js`` (``gateway_publish``), or a bare ``XADD`` for the
  inputs both of those refuse (``xadd``).
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import tempfile
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import pytest

from workitems import a2a_archive, command_consumer, registry, store
from workitems.envelope import Kind, build_envelope, to_stream_fields
from workitems.models import A2AArtifact, A2AConflict, A2AMessage, A2ATask, A2AVerdict
from workitems.stream_topology import COMMAND_GROUP, command_stream_name
from workitems.streams import create_consumer, publish

from tests.wait_support import wait_for

CORE_DIR = Path(__file__).resolve().parent.parent
SOURCE_DIR = CORE_DIR.parent.parent
SCRUMMASTER_DIR = SOURCE_DIR / 'services' / 'scrummaster'
TOOLS_DIR = SOURCE_DIR / 'setup' / 'commons' / 'tools'
DRIVER = Path(__file__).resolve().parent / 'a2a_scrummaster_driver.js'

# The fixture catalog (services/scrummaster/test/fixtures): one project, two
# agents — backend-agent on `backend`, refinement-agent on `refinement`.
PROJECT = 'test-project'
GATEWAY_STREAM = registry.gateway_stream_name(PROJECT)
BACKEND_STREAM = registry.agent_stream_name(PROJECT, 'backend')
REFINEMENT_STREAM = registry.agent_stream_name(PROJECT, 'refinement')
A2A_TABLES = ('a2a_task', 'a2a_message', 'a2a_artifact', 'a2a_conflict', 'a2a_verdict')

FAST = {'retry_delay_ms': 150, 'reclaim_interval_ms': 100, 'block_ms': 100}


def redis_host_port() -> tuple[str, str]:
    url = urlparse(os.environ['REDIS_TEST_URL'])
    return url.hostname or 'localhost', str(url.port or 6379)


@pytest.fixture
def a2a_db(clean_db):
    """``clean_db`` plus the A2A tables, which conftest's list predates.
    TRUNCATE fires no row trigger, so the append-only triggers allow it."""
    from django.db import connection
    with connection.cursor() as cursor:
        cursor.execute(f"TRUNCATE {', '.join(A2A_TABLES)} CASCADE")
    yield


def new_work_item(project: str = PROJECT, display_name: str = 'A2A record test') -> str:
    item_id = str(uuid.uuid4())
    store.create_work_item({'id': item_id, 'project': project, 'type': 'task', 'displayName': display_name},
                           actor='tester')
    return item_id


# ---------------------------------------------------------------------------
# core's consumers
# ---------------------------------------------------------------------------

class CoreConsumers:
    """The archive consumers and the command consumer, started and stopped
    independently, as the Acceptance steps that stop one of them need."""

    def __init__(self, redis_factory, *, project: str = PROJECT, timing: dict | None = None):
        self.redis_factory = redis_factory
        self.project = project
        self.timing = {**FAST, **(timing or {})}
        self.archive: list = []
        self.command = None

    def start_archive(self, streams: list[str] | None = None):
        consumers = a2a_archive.create_archive_consumers(self.redis_factory, consumer_name='a2a-test', **self.timing)
        for consumer in consumers:
            if streams is None or consumer.stream in streams:
                consumer.start()
                self.archive.append(consumer)
        return self

    def stop_archive(self, streams: list[str] | None = None):
        keep = []
        for consumer in self.archive:
            if streams is None or consumer.stream in streams:
                consumer.stop()
            else:
                keep.append(consumer)
        self.archive = keep
        return self

    def start_command(self, handler=None):
        self.command = create_consumer(
            self.redis_factory, stream=command_stream_name(self.project), group=COMMAND_GROUP,
            consumer_name='a2a-test', handler=handler or command_consumer._handler, **self.timing,
        )
        self.command.start()
        return self

    def stop_command(self):
        if self.command is not None:
            self.command.stop()
        self.command = None
        return self

    def stop(self):
        self.stop_archive()
        self.stop_command()


@pytest.fixture
def core(redis_client, redis_factory):
    consumers = CoreConsumers(redis_factory)
    try:
        yield consumers
    finally:
        consumers.stop()


# ---------------------------------------------------------------------------
# ScrumMaster, for real, in a Node child process
# ---------------------------------------------------------------------------

class ScrumMaster:
    def __init__(self, *, catalog_path: str | None = None, projects_path: str | None = None):
        from django.conf import settings
        host, port = redis_host_port()
        env = {
            **os.environ,
            'REDIS_HOST': host,
            'REDIS_PORT': port,
            # The catalog core's registry reads, so both sides see one topology.
            'AGENTS_CATALOG_PATH': catalog_path or settings.AGENTS_CATALOG_PATH or os.environ['AGENTS_CATALOG_PATH'],
            'PROJECTS_CONFIG_PATH': projects_path or settings.PROJECTS_CONFIG_PATH or os.environ['PROJECTS_CONFIG_PATH'],
            # No core HTTP API runs in these tests: the gateway's one read
            # (a work item's status, for a log line) degrades to "not known".
            'WORKITEM_SERVICE_URL': 'http://127.0.0.1:9',
        }
        self.stderr = tempfile.TemporaryFile(mode='w+')
        self.process = subprocess.Popen(
            ['node', str(DRIVER)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr,
            text=True, env=env, cwd=str(SCRUMMASTER_DIR),
        )
        self._replies: queue.Queue = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()
        ready = self._next(20)
        assert ready.get('ready'), ready

    def _read(self):
        for line in self.process.stdout:
            self._replies.put(json.loads(line))
        self._replies.put({'ok': False, 'error': 'the ScrumMaster driver exited'})

    def _next(self, timeout_s):
        try:
            return self._replies.get(timeout=timeout_s)
        except queue.Empty:
            raise AssertionError(f'no reply from the ScrumMaster driver within {timeout_s}s; '
                                 f'its log:\n{self.log()}') from None

    def log(self) -> str:
        self.stderr.seek(0)
        return self.stderr.read()

    def request(self, op: str, timeout_s: float = 20, **fields) -> dict:
        self.process.stdin.write(json.dumps({'op': op, **fields}) + '\n')
        self.process.stdin.flush()
        reply = self._next(timeout_s)
        if not reply.get('ok'):
            raise AssertionError(f'ScrumMaster driver {op} failed: {reply.get("error")}\nlog:\n{self.log()}')
        return reply

    def dispatch(self, work_item_id: str, agent_id: str = 'backend-agent', *, project: str = PROJECT,
                 dispatch_id: str | None = None) -> dict:
        return self.request('dispatch', workItemId=work_item_id, project=project, agentId=agent_id,
                            dispatchId=dispatch_id)

    def start_gateway(self, project: str = PROJECT, **timing) -> None:
        self.request('startGateway', project=project, **timing)

    def stop_gateway(self, project: str | None = None) -> None:
        self.request('stopGateway', project=project)

    def replay(self, stream: str, dead_entry_id: str) -> str:
        return self.request('replay', stream=stream, deadEntryId=dead_entry_id)['entryId']

    def fail_publishes(self, count: int, which: str = 'projection') -> None:
        self.request('failPublishes', count=count, which=which)

    def task_state(self, task_id: str) -> dict:
        return self.request('taskState', taskId=task_id)

    def close(self) -> None:
        if self.process.poll() is None:
            try:
                self.request('stopGateway', timeout_s=10)
            except AssertionError:
                pass
            self.process.stdin.close()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.stderr.close()


@contextmanager
def scrummaster():
    instance = ScrumMaster()
    try:
        yield instance
    finally:
        instance.close()


# ---------------------------------------------------------------------------
# An agent
# ---------------------------------------------------------------------------

def submit_tool(dispatched: dict, args: list[str], *, session_id: str = 'a2a-test-session',
                project: str = PROJECT, state_dir: str | None = None, unset: tuple = ()) -> subprocess.CompletedProcess:
    """Runs the real submit tool as the dispatched agent would: the dispatch
    context the subscriber exports, the chain state beside a commons dir, and
    the harness session id. ``state_dir`` keeps the chain across calls of one
    session."""
    # One agent session per dispatch unless told otherwise, so the tool's
    # chain carries from one submission to the next as it does in a run.
    session_root = state_dir or dispatched.setdefault('sessionDir', tempfile.mkdtemp(prefix='a2a-session-'))
    os.makedirs(os.path.join(session_root, 'state'), exist_ok=True)
    os.makedirs(os.path.join(session_root, 'commons'), exist_ok=True)
    host, port = redis_host_port()
    env = {
        **os.environ,
        'PROJECT_NAME': project,
        'A2A_TASK_ID': dispatched['taskId'],
        'A2A_CONTEXT_ID': dispatched['contextId'],
        'A2A_LAST_MESSAGE_ID': dispatched['messageId'],
        'AIGANG_COMMONS_DIR': os.path.join(session_root, 'commons'),
        'REDIS_HOST': host,
        'REDIS_PORT': port,
        'NODE_PATH': str(SCRUMMASTER_DIR / 'node_modules'),
        'CLAUDE_CODE_SESSION_ID': session_id,
    }
    for name in unset:
        env.pop(name, None)
    return subprocess.run(['node', str(TOOLS_DIR / 'a2a-submit.js'), *args], env=env, capture_output=True,
                          text=True, timeout=30)


def last_entry(redis_client, stream: str) -> tuple[str, dict]:
    entry_id, fields = redis_client.xrevrange(stream, count=1)[0]
    return entry_id, json.loads(fields['data'])


def submitted(redis_client, result: subprocess.CompletedProcess, stream: str = GATEWAY_STREAM) -> tuple[str, dict]:
    assert result.returncode == 0, f'the submit tool refused: {result.stderr}'
    return last_entry(redis_client, stream)


def gateway_publish(payload: dict, *, project: str = PROJECT) -> subprocess.CompletedProcess:
    """The raw entry point, ``gateway-publish.js <project> -``."""
    host, port = redis_host_port()
    env = {**os.environ, 'REDIS_HOST': host, 'REDIS_PORT': port, 'NODE_PATH': str(SCRUMMASTER_DIR / 'node_modules')}
    result = subprocess.run(['node', str(TOOLS_DIR / 'gateway-publish.js'), project, '-'], input=json.dumps(payload),
                            env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, f'gateway-publish.js refused: {result.stderr}'
    return result


def agent_message(task_id: str, context_id: str | None, *, text: str = 'note', data: dict | None = None,
                  reference_message_id: str | None = None, message_id: str | None = None, **extra) -> dict:
    parts = [{'kind': 'text', 'text': text}]
    if data is not None:
        parts.append({'kind': 'data', 'data': data})
    message = {'kind': 'message', 'messageId': message_id or str(uuid.uuid4()), 'taskId': task_id,
               'role': 'agent', 'parts': parts}
    if context_id is not None:
        message['contextId'] = context_id
    if reference_message_id:
        message['referenceMessageId'] = reference_message_id
    message.update(extra)
    return message


def gateway_envelope(task_id: str | None, payload: dict, *, context_id: str | None = None,
                     kind: str = Kind.GATEWAY_OPERATION, project: str = PROJECT, **fields) -> dict:
    envelope = build_envelope(kind, project, task_id=task_id, context_id=context_id, payload=payload)
    envelope.update(fields)
    return envelope


def xadd(redis_client, stream: str, envelope: dict, entry_id: str = '*') -> str:
    return redis_client.xadd(stream, to_stream_fields(envelope), id=entry_id)


def publish_verdict(redis_client, payload: dict, *, project: str = PROJECT, message_id: str | None = None,
                    created_at: str | None = None) -> dict:
    """A ``recordA2AVerdict`` command onto the project's command stream, in the
    envelope ``canonicalWorkItems.publishCommand`` builds."""
    envelope = build_envelope(Kind.WORK_ITEM_COMMAND, project,
                              payload={'command': 'recordA2AVerdict', 'actor': 'scrummaster', **payload},
                              message_id=message_id, created_at=created_at)
    publish(redis_client, command_stream_name(project), envelope)
    return envelope


# ---------------------------------------------------------------------------
# Reading back
# ---------------------------------------------------------------------------

def record(task_id: str) -> dict | None:
    return store.a2a_task_record(task_id)


def message_view(task_id: str, message_id: str) -> dict | None:
    rec = record(task_id)
    for message in (rec or {}).get('messages', []):
        if message['messageId'] == message_id:
            return message
    return None


def stored(message_id: str) -> bool:
    return A2AMessage.objects.filter(message_id=message_id).exists()


def wait_stored(message_id: str, timeout_s: float = 10):
    wait_for(lambda: stored(message_id), timeout_s=timeout_s,
             expected=f'A2A message {message_id} stored by core-a2a-archive',
             observed=lambda: list(A2AMessage.objects.values_list('message_id', flat=True)))


def verdict_rows(message_id: str) -> list[A2AVerdict]:
    return list(A2AVerdict.objects.filter(message_id=message_id).order_by('command_created_at', 'recorded_at'))


def wait_verdict_rows(message_id: str, count: int, timeout_s: float = 10):
    wait_for(lambda: len(verdict_rows(message_id)) >= count, timeout_s=timeout_s,
             expected=f'{count} verdict row(s) for {message_id}',
             observed=lambda: [(r.verdict, r.reason, r.outcome) for r in verdict_rows(message_id)])


def wait_reading(task_id: str, message_id: str, *, verdict=..., outcome=..., timeout_s: float = 10):
    """Waits until REQ-06's read gives the message (archived or not) the
    verdict and/or outcome named."""
    def current():
        rec = record(task_id) or {}
        for m in rec.get('messages', []) + rec.get('unarchivedVerdicts', []):
            if m['messageId'] == message_id:
                return m
        return None

    def holds():
        m = current()
        return m is not None and (verdict is ... or m['verdict'] == verdict) \
            and (outcome is ... or m['outcome'] == outcome)

    wait_for(holds, timeout_s=timeout_s,
             expected=f'{message_id} reading verdict={verdict!r} outcome={outcome!r}',
             observed=lambda: {k: (current() or {}).get(k) for k in ('verdict', 'reason', 'outcome')})
    return current()


def pending_in_archive_group(redis_client, stream: str) -> int:
    try:
        return redis_client.xpending(stream, a2a_archive.A2A_ARCHIVE_GROUP)['pending']
    except Exception:  # noqa: BLE001 - no group yet means nothing pending
        return 0


def wait_archive_drained(redis_client, stream: str, timeout_s: float = 10):
    """Every entry on ``stream`` delivered to and acknowledged by
    ``core-a2a-archive``: none pending and none undelivered."""
    def drained():
        groups = {g['name']: g for g in redis_client.xinfo_groups(stream)}
        group = groups.get(a2a_archive.A2A_ARCHIVE_GROUP)
        return group is not None and group['pending'] == 0 and (group.get('lag') or 0) == 0
    wait_for(drained, timeout_s=timeout_s, expected=f'core-a2a-archive drained on {stream}',
             observed=lambda: redis_client.xinfo_groups(stream))


def outbox_rows(event_type: str, **payload_match):
    from workitems.models import OutboxEvent
    rows = OutboxEvent.objects.filter(event_type=event_type).order_by('created_at')
    return [r for r in rows if all(r.payload.get(k) == v for k, v in payload_match.items())]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    'A2AArtifact', 'A2AConflict', 'A2AMessage', 'A2ATask', 'A2AVerdict', 'BACKEND_STREAM', 'CoreConsumers',
    'FAST', 'GATEWAY_STREAM', 'PROJECT', 'REFINEMENT_STREAM', 'ScrumMaster', 'a2a_db', 'agent_message', 'core',
    'gateway_envelope', 'gateway_publish', 'last_entry', 'message_view', 'new_work_item', 'now_iso', 'outbox_rows',
    'pending_in_archive_group', 'publish_verdict', 'record', 'scrummaster', 'stored', 'submit_tool', 'submitted',
    'verdict_rows', 'wait_archive_drained', 'wait_for', 'wait_reading', 'wait_stored', 'wait_verdict_rows', 'xadd',
]
