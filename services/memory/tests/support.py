"""Builders for harness records and session files, shaped as the harness
binding describes them (Management `strategy/bindings/claude-code.md`):
records a 2.1.296 project-container transcript opens with, prompt and end
records, `pr-link`, and Bash tool calls."""

import json
import uuid
from pathlib import Path

VERSION = '2.1.296'
PROJECT = 'proj'
CWD = '-workspace'


def line(record):
    return (json.dumps(record) + '\n').encode()


def conv(rtype='assistant', version=VERSION, session_id='s', content=None, **extra):
    record = {'type': rtype, 'uuid': str(uuid.uuid4()), 'parentUuid': None, 'sessionId': session_id,
              'timestamp': '2026-10-12T12:00:00Z', 'version': version,
              'message': {'role': 'assistant' if rtype == 'assistant' else 'user', 'content': content or []}}
    record.update(extra)
    return record


def enqueue(content='-\n## ROLE\nDo the work.'):
    return {'type': 'queue-operation', 'operation': 'enqueue', 'content': content,
            'timestamp': '2026-10-12T12:00:00Z', 'sessionId': 's'}


def dequeue():
    return {'type': 'queue-operation', 'operation': 'dequeue', 'timestamp': '2026-10-12T12:00:00Z', 'sessionId': 's'}


def cost_state():
    return {'type': 'cost-state', 'sessionId': 's'}


def pr_link(number, repository='org/repo'):
    return {'type': 'pr-link', 'prNumber': number, 'prRepository': repository,
            'prUrl': f'https://github.com/{repository}/pull/{number}', 'sessionId': 's',
            'timestamp': '2026-10-12T12:05:00Z'}


def bash(tool_use_id, command):
    return conv('assistant', content=[{'type': 'tool_use', 'id': tool_use_id, 'name': 'Bash',
                                       'input': {'command': command}}])


def tool_call(tool_use_id, name, tool_input):
    return conv('assistant', content=[{'type': 'tool_use', 'id': tool_use_id, 'name': name, 'input': tool_input}])


def result(tool_use_id, text, as_list=False):
    content = [{'type': 'text', 'text': text}] if as_list else text
    return conv('user', content=[{'type': 'tool_result', 'tool_use_id': tool_use_id, 'content': content}])


def opening(prompt='-\n## ROLE\nDo the work.'):
    """A run's opening records in a project container: enqueue, dequeue,
    user (orchestrator's confirmation, run 20261010-131150)."""
    return [enqueue(prompt), dequeue(), conv('user')]


def completed_run(prompt='-\n## ROLE\nDo the work.'):
    return opening(prompt) + [conv('assistant'), {'type': 'last-prompt', 'sessionId': 's'}, cost_state()]


class Session:
    """One session's files on a temporary harness volume."""

    def __init__(self, harness, session_id=None, project=PROJECT):
        self.harness = Path(harness)
        self.project = project
        self.id = session_id or str(uuid.uuid4())
        self.dir = self.harness / project / CWD
        self.dir.mkdir(parents=True, exist_ok=True)

    @property
    def transcript(self):
        return self.dir / f'{self.id}.jsonl'

    def sidecar(self, *rel):
        return self.dir.joinpath(self.id, *rel)

    def parts(self, path):
        return Path(path).relative_to(self.harness).parts

    def append(self, records, path=None):
        path = Path(path or self.transcript)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'ab') as fh:
            for record in records:
                fh.write(record if isinstance(record, bytes) else line(record))
        return path

    def write(self, rel, data):
        path = self.sidecar(*rel.split('/'))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def notes_dir(self):
        d = self.dir / 'memory'
        d.mkdir(parents=True, exist_ok=True)
        return d


def archive_bytes(archive, harness, path):
    return (Path(archive) / Path(path).relative_to(harness)).read_bytes()
