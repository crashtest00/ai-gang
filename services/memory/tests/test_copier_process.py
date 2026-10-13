"""REQ-03's triggers through memory-copier's own process (`manage.py
run_copier`): file-change events, the watcher disabled, an exhausted watch
limit, and the refusal of a pass interval that is not below the quiet
interval."""

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from django.core.management import call_command
from django.db import connection

from tests.support import Session as Src, conv, opening

pytestmark = pytest.mark.django_db(transaction=True)
MEMORY_DIR = Path(__file__).resolve().parent.parent


def run_copier_in_background(seconds):
    errors = []

    def target():
        try:
            call_command('run_copier', max_seconds=seconds)
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)
        finally:
            connection.close()

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, errors


def wait_for(predicate, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def archived(archive, harness, path):
    target = Path(archive) / Path(path).relative_to(harness)
    return target.read_bytes() if target.exists() else None


@pytest.fixture
def fast(settings):
    settings.MEMORY_EVENT_SETTLE_SECONDS = 0.05
    settings.MEMORY_QUIET_SWEEP_SECONDS = 600
    settings.MEMORY_RECONCILE_INTERVAL_SECONDS = 600
    settings.MEMORY_WATCHER_ENABLED = True
    settings.MEMORY_MAX_WATCHES = None
    return settings


def test_file_change_events_copy_a_session_as_it_is_written(roots, fast):
    harness, archive = roots
    thread, errors = run_copier_in_background(6)
    time.sleep(1)
    s = Src(harness)  # a new project directory and session after startup
    s.append(opening())
    assert wait_for(lambda: archived(archive, harness, s.transcript) == s.transcript.read_bytes())
    s.append([conv('assistant')], path=s.sidecar('subagents', 'agent-e.jsonl'))
    agent = s.sidecar('subagents', 'agent-e.jsonl')
    assert wait_for(lambda: archived(archive, harness, agent) == agent.read_bytes())
    thread.join(10)
    assert not errors


def test_with_the_watcher_disabled_a_new_line_waits_for_the_next_pass(roots, fast):
    harness, archive = roots
    fast.MEMORY_WATCHER_ENABLED = False
    fast.MEMORY_RECONCILE_INTERVAL_SECONDS = 2
    s = Src(harness)
    s.append(opening())
    thread, errors = run_copier_in_background(5)
    assert wait_for(lambda: archived(archive, harness, s.transcript) == s.transcript.read_bytes(), 3)
    s.append([conv('assistant')])
    time.sleep(0.5)
    assert archived(archive, harness, s.transcript) != s.transcript.read_bytes()
    assert wait_for(lambda: archived(archive, harness, s.transcript) == s.transcript.read_bytes(), 4)
    thread.join(10)
    assert not errors


def test_an_exhausted_watch_limit_falls_back_to_the_pass(roots, fast):
    harness, archive = roots
    fast.MEMORY_MAX_WATCHES = 0
    fast.MEMORY_RECONCILE_INTERVAL_SECONDS = 2
    s = Src(harness)
    s.append(opening())
    thread, errors = run_copier_in_background(5)
    assert wait_for(lambda: archived(archive, harness, s.transcript) == s.transcript.read_bytes(), 3)
    s.append([conv('assistant')])
    assert wait_for(lambda: archived(archive, harness, s.transcript) == s.transcript.read_bytes(), 4)
    thread.join(10)
    assert not errors


@pytest.mark.parametrize('command', [['check'], ['run_copier', '--once']])
def test_a_pass_interval_of_35_minutes_is_refused_naming_the_setting(command):
    env = {**os.environ, 'MEMORY_RECONCILE_INTERVAL_SECONDS': str(35 * 60)}
    proc = subprocess.run([sys.executable, 'manage.py', *command], cwd=MEMORY_DIR, env=env,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode != 0
    assert 'MEMORY_RECONCILE_INTERVAL_SECONDS' in proc.stderr


def test_a_pass_interval_below_the_quiet_interval_is_accepted():
    env = {**os.environ, 'MEMORY_RECONCILE_INTERVAL_SECONDS': str(30 * 60)}
    proc = subprocess.run([sys.executable, 'manage.py', 'check'], cwd=MEMORY_DIR, env=env,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
