"""
Runs the librarian's Streams subscriber and blocks until SIGTERM/SIGINT —
the same shape as ``workitems``' own ``run_consumers``, and the command
``docker-compose.yml``'s ``librarian`` service runs.

One consumer, not one per project: the request stream is instance-wide
(``librarian/stream_topology.py``), because a request names its
destination repository in the payload rather than in the stream name.
"""

from __future__ import annotations

import signal
import time

from django.conf import settings
from django.core.management.base import BaseCommand

from librarian.consumer import create_librarian_consumer
from librarian.stream_topology import REQUEST_GROUP, REQUEST_STREAM
from workitems.redis_client import new_client


class Command(BaseCommand):
    help = 'Run the librarian artifact-delivery subscriber.'

    def add_arguments(self, parser):
        parser.add_argument('--consumer-id', default=None, help='Override WORKITEM_CONSUMER_ID for this process.')

    def handle(self, *args, **options):
        consumer_name = options.get('consumer_id') or settings.WORKITEM_CONSUMER_ID
        consumer = create_librarian_consumer(new_client, consumer_name=consumer_name)
        consumer.start()
        self.stdout.write(self.style.SUCCESS(
            f'[librarian] consuming {REQUEST_STREAM} as group "{REQUEST_GROUP}" '
            f'(projects root {settings.PROJECTS_ROOT}, artifacts {settings.ARTIFACT_ROOT})'
        ))

        stop = {'flag': False}

        def _shutdown(signum, frame):
            stop['flag'] = True

        signal.signal(signal.SIGTERM, _shutdown)
        signal.signal(signal.SIGINT, _shutdown)

        try:
            while not stop['flag']:
                time.sleep(0.5)
        finally:
            self.stdout.write('[librarian] shutting down')
            consumer.stop()
