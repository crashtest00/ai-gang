"""
Standalone outbox relay process. Whether the outbox relay runs as a
separate deployable process or in-process within the service is an
implementation detail; this repo runs it as a separate process, `python
manage.py relay`, so it can be killed and restarted independently of the
HTTP/consumer process, which is also what
tests/test_relay_integration.py exercises.

Env (read via Django settings, which read them from the environment —
see workitemservice/settings.py): PGHOST/PGPORT/PGUSER/PGPASSWORD/
PGDATABASE (or DATABASE_URL), REDIS_URL (or REDIS_HOST/REDIS_PORT),
RELAY_POLL_INTERVAL_MS, RELAY_BATCH_SIZE, RELAY_ROW_DELAY_MS (test-only —
see workitems/relay.py).
"""

from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand

from workitems.redis_client import new_client
from workitems.relay import run_loop


class Command(BaseCommand):
    help = 'Run the transactional outbox relay loop.'

    def handle(self, *args, **options):
        redis_client = new_client()
        self.stdout.write(self.style.SUCCESS('[relay] starting outbox relay loop'))
        run_loop(
            redis_client,
            poll_interval_ms=settings.RELAY_POLL_INTERVAL_MS,
            batch_size=settings.RELAY_BATCH_SIZE,
            row_delay_ms=settings.RELAY_ROW_DELAY_MS,
        )
