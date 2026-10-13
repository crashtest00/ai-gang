"""
``python manage.py relay`` with its ``published_at`` update made to fail
once for each row, after ``publish`` has returned — durable-artifact-event-
history.md REQ-05's Acceptance.

That is the state a crash between the relay's ``XADD`` and its update
leaves, a window too short to hit with a kill. Which rows have already
failed is remembered in the file named by the first argument, one row id
per line, so the failure happens once per row across restarts. Nothing in
the relay catches the failure (``relay_once``, ``run_loop``), so it stops
the process, and the caller restarts it until every row has failed once.

Everything else is the real command: the same settings, the same
``run_loop``, the same ``publish`` and dedupe key. Run as a script by
``test_artifact_event_history_relay_restart.py``; not collected by pytest
(``pytest.ini``'s ``python_files = test_*.py``).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

CORE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CORE_ROOT))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')

import django  # noqa: E402

django.setup()

from django.core.management import call_command  # noqa: E402
from django.db import DatabaseError  # noqa: E402

from workitems import relay  # noqa: E402
from workitems.models import OutboxEvent  # noqa: E402

FAILED_ROWS = Path(sys.argv[1])


def _already_failed() -> set:
    return set(FAILED_ROWS.read_text().split()) if FAILED_ROWS.exists() else set()


class _UpdateThatFailsOnce:
    def __init__(self, queryset, row_id):
        self._queryset = queryset
        self._row_id = str(row_id)

    def update(self, **fields):
        if self._row_id not in _already_failed():
            with FAILED_ROWS.open('a') as record:
                record.write(f'{self._row_id}\n')
            raise DatabaseError(f'published_at update for outbox row {self._row_id} failed after publish')
        return self._queryset.update(**fields)


class _Objects:
    def filter(self, *args, **kwargs):
        queryset = OutboxEvent.objects.filter(*args, **kwargs)
        if set(kwargs) == {'id'}:  # relay_once's published_at update, relay.py's only filter(id=...)
            return _UpdateThatFailsOnce(queryset, kwargs['id'])
        return queryset


class _OutboxEventWithFailingUpdate:
    objects = _Objects()


relay.OutboxEvent = _OutboxEventWithFailingUpdate
call_command('relay')
