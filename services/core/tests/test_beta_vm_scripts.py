"""
canonical-delivery-state.md REQ-08 — the Beta VM's forced command and
preview scripts take the canonical work-item id, and no tracker-key shape
is accepted or produced any more.

Each script is RUN here (as test_field_id_scripts.py runs the field-id
provisioning scripts), against a stubbed `docker` that records every
invocation and reports success, so the script's own argument-validation and
label/filter wiring is the enforcement point — not a Python re-implementation
of its regex. No test here talks to a Beta VM.
"""

from __future__ import annotations

import os
import stat
import subprocess
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
DEPLOY_DIR = REPO_ROOT / 'beta-vm' / 'deploy'

A_WORK_ITEM_ID = str(uuid.uuid4())
A_JIRA_KEY = 'GANG-42'  # the shape REQ-08 retires — ISSUE_KEY_RE used to accept this


@pytest.fixture
def docker_stub(tmp_path):
    """A PATH whose `docker` records its full argument list (one line per
    invocation) to DOCKER_LOG and answers every call as a success: `image
    inspect` reports the image exists, `run`/`rm` succeed, and `ps -a
    --filter ...` echoes back one matching container name derived from
    this fixture's own `--filter name=` argument, so a test can drive a
    teardown's removal branch without a real Docker daemon."""
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    log_file = tmp_path / 'docker.log'
    docker = bin_dir / 'docker'
    docker.write_text(
        '#!/usr/bin/env bash\n'
        f'echo "$@" >> "{log_file}"\n'
        'case "$1" in\n'
        '  image) exit 0 ;;\n'
        '  rm) exit 0 ;;\n'
        '  run) echo "fake-container-id"; exit 0 ;;\n'
        '  ps)\n'
        '    name_filter=""\n'
        '    for arg in "$@"; do\n'
        '      case "$arg" in name=*) name_filter="${arg#name=}" ;; esac\n'
        '    done\n'
        '    echo "${name_filter}fake-sha"\n'
        '    ;;\n'
        '  *) exit 0 ;;\n'
        'esac\n'
    )
    docker.chmod(0o755)
    return bin_dir, log_file


def run_forced_command(command: str, docker_stub, *, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    bin_dir, _log_file = docker_stub
    env = {
        **os.environ,
        'PATH': f'{bin_dir}:{os.environ["PATH"]}',
        'SSH_ORIGINAL_COMMAND': command,
        'PREVIEW_DOMAIN': 'preview.example.com',
        **(extra_env or {}),
    }
    return subprocess.run(
        ['bash', str(DEPLOY_DIR / 'forced-command.sh')],
        env=env, capture_output=True, text=True, timeout=30,
    )


def test_preview_deploy_accepts_the_canonical_work_item_id(docker_stub):
    _bin_dir, log_file = docker_stub
    result = run_forced_command(f'preview-deploy hello-world abc1234 {A_WORK_ITEM_ID}', docker_stub)

    assert result.returncode == 0, result.stderr
    assert 'Preview live:' in result.stdout
    log = log_file.read_text()
    assert f'work_item={A_WORK_ITEM_ID}' in log, 'the preview container is labelled by the canonical id'
    assert 'issue=' not in log, 'no tracker-key label is written any more (REQ-08)'


def test_preview_deploy_refuses_a_jira_key_shaped_third_argument(docker_stub):
    result = run_forced_command(f'preview-deploy hello-world abc1234 {A_JIRA_KEY}', docker_stub)

    assert result.returncode == 1
    assert 'Refused' in result.stderr
    _bin_dir, log_file = docker_stub
    assert not log_file.exists() or log_file.read_text() == '', \
        'a refused command execs nothing, so docker is never invoked'


def test_preview_teardown_by_work_item_accepts_the_canonical_id_and_filters_on_it(docker_stub):
    _bin_dir, log_file = docker_stub
    result = run_forced_command(f'preview-teardown-by-work-item hello-world {A_WORK_ITEM_ID}', docker_stub)

    assert result.returncode == 0, result.stderr
    assert 'Torn down' in result.stdout
    log = log_file.read_text()
    assert f'label=work_item={A_WORK_ITEM_ID}' in log
    assert 'label=issue=' not in log, 'no tracker-key filter is used any more (REQ-08)'


def test_preview_teardown_by_work_item_refuses_a_jira_key_shaped_second_argument(docker_stub):
    result = run_forced_command(f'preview-teardown-by-work-item hello-world {A_JIRA_KEY}', docker_stub)

    assert result.returncode == 1
    assert 'Refused' in result.stderr


def test_the_retired_operation_name_is_gone():
    """`preview-teardown-by-issue` (the SSH operation name, not just the
    script file) is renamed with its script (REQ-08)."""
    text = (DEPLOY_DIR / 'forced-command.sh').read_text()
    assert 'preview-teardown-by-issue' not in text
    assert 'preview-teardown-by-work-item' in text
