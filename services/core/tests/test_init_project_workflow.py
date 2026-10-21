"""
release-mode-parity.md REQ-14 — the Jira workflow `scripts/init-project.sh`
provisions has an `Abandoned` status (category Done), a global `Abandon`
transition, and a `Done` transition offered from every status but
`Abandoned`; `ensure_workflow_statuses` creates the status when the
instance lacks it.

The two functions are RUN here, extracted out of the script itself, against
a stubbed `curl` that records every request body and answers the two Jira
reads/writes they make. That is the enforcement point: the REQ is about what
an operator's run sends to Jira, not about what the script's text says. No
test here talks to a Jira.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
INIT_PROJECT = REPO_ROOT / 'scripts' / 'init-project.sh'

# The statuses an instance that predates v5.3 already has globally.
EXISTING = [
    ('Backlog', '1'), ('In Progress', '3'), ('Done', '5'),
    ('Shovel Ready', '2'), ('In Review', '4'),
]
SHELL_PRELUDE = '''
set -u
AUTH="u:t"; API="https://jira.example/rest/api/3"
FUNCS=$(sed -n '/^jira_get()/,/^}/p;/^jira_post()/,/^}/p;/^ensure_workflow_statuses()/,/^}/p;/^ensure_ai_gang_workflow()/,/^}/p' "$INIT_SCRIPT")
eval "$FUNCS"
WORKFLOW_NAME="AI Gang Kanban"
STATUS_BACKLOG=""; STATUS_SHOVEL_READY=""; STATUS_IN_PROGRESS=""
STATUS_IN_REVIEW=""; STATUS_DONE=""; STATUS_ABANDONED=""
'''


def make_curl(bin_dir: Path, log: Path, existing, workflow_reply: str) -> None:
    values = json.dumps({'values': [{'name': n, 'id': i} for n, i in existing]})
    curl = bin_dir / 'curl'
    curl.write_text(
        '#!/usr/bin/env bash\n'
        'url=""; body=""; method=GET\n'
        'while [[ $# -gt 0 ]]; do\n'
        '  case "$1" in\n'
        '    -X) method="$2"; shift 2;;\n'
        '    -d) body="$2"; shift 2;;\n'
        '    -u|-H) shift 2;;\n'
        '    *) url="$1"; shift;;\n'
        '  esac\n'
        'done\n'
        # The script's JSON bodies are pretty-printed; keep one request per line.
        f'echo "$method $url $(jq -c . <<<"${{body:-null}}")" >> {log}\n'
        'case "$method $url" in\n'
        f"  'GET '*/statuses/search) echo '{values}';;\n"
        # POST /statuses: echo back an id per created status name.
        '  "POST "*/statuses) echo "$body" | jq \'[to_entries[] | {name: .value.name, id: ("new-" + .value.name)}]\';;\n'
        f"  'POST '*/workflow) echo '{workflow_reply}';;\n"
        '  *) echo "unexpected: $method $url" >&2; exit 9;;\n'
        'esac\n'
    )
    curl.chmod(0o755)


def run_functions(tmp_path, existing, workflow_reply='{"entityId": "wf-1"}'):
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    log = tmp_path / 'requests.log'
    log.write_text('')
    make_curl(bin_dir, log, existing, workflow_reply)
    script = SHELL_PRELUDE + '''
ensure_workflow_statuses
ensure_ai_gang_workflow
echo "ABANDONED_ID=$STATUS_ABANDONED"
'''
    result = subprocess.run(
        ['bash', '-c', script],
        env={**os.environ, 'PATH': f'{bin_dir}:{os.environ["PATH"]}', 'INIT_SCRIPT': str(INIT_PROJECT)},
        capture_output=True, text=True, timeout=60,
    )
    requests = [line.split(' ', 2) for line in log.read_text().splitlines()]
    return result, requests


def posted(requests, suffix):
    return [json.loads(body) for method, url, body in requests
            if method == 'POST' and url.endswith(suffix)]


def test_missing_abandoned_status_is_created_with_category_done(tmp_path):
    result, requests = run_functions(tmp_path, EXISTING)
    assert result.returncode == 0, result.stderr
    created = posted(requests, '/statuses')
    assert len(created) == 1
    assert {'name': 'Abandoned', 'statusCategory': 'DONE'} in created[0]
    # Shovel Ready and In Review exist, so only Abandoned is created.
    assert [s['name'] for s in created[0]] == ['Abandoned']
    assert 'ABANDONED_ID=new-Abandoned' in result.stdout


def test_existing_abandoned_status_is_reused_not_recreated(tmp_path):
    result, requests = run_functions(tmp_path, EXISTING + [('Abandoned', '6')])
    assert result.returncode == 0, result.stderr
    assert posted(requests, '/statuses') == []
    assert 'ABANDONED_ID=6' in result.stdout


def test_workflow_has_abandoned_status_and_global_abandon_transition(tmp_path):
    result, requests = run_functions(tmp_path, EXISTING + [('Abandoned', '6')])
    assert result.returncode == 0, result.stderr
    (workflow,) = posted(requests, '/workflow')
    assert {'id': '6'} in workflow['statuses']
    abandon = [t for t in workflow['transitions'] if t['name'] == 'Abandon']
    assert abandon == [{'name': 'Abandon', 'to': '6', 'type': 'global', 'from': []}]


def test_done_is_offered_from_every_status_but_abandoned(tmp_path):
    result, requests = run_functions(tmp_path, EXISTING + [('Abandoned', '6')])
    assert result.returncode == 0, result.stderr
    (workflow,) = posted(requests, '/workflow')
    (done,) = [t for t in workflow['transitions'] if t['name'] == 'Done']
    assert done['to'] == '5'
    assert done['type'] != 'global'
    # Backlog, Shovel Ready, In Progress, In Review: every status but
    # Done itself (the target) and Abandoned.
    assert sorted(done['from']) == ['1', '2', '3', '4']
    assert '6' not in done['from']
    # The other transitions keep their global reach.
    for name in ('Backlog', 'Shovel Ready', 'Start', 'In Review', 'Abandon'):
        (t,) = [t for t in workflow['transitions'] if t['name'] == name]
        assert t['type'] == 'global'


def test_an_existing_workflow_is_skipped_not_reprovisioned(tmp_path):
    result, requests = run_functions(
        tmp_path, EXISTING + [('Abandoned', '6')],
        workflow_reply='{"errorMessages": ["A workflow with this name already exists."]}')
    assert result.returncode == 0, result.stderr
    assert 'already exists' in result.stdout
    # One create attempt, and nothing else written to the workflow.
    assert len(posted(requests, '/workflow')) == 1
    assert [r for r in requests if r[0] != 'GET' and not r[1].endswith('/workflow')] == []
