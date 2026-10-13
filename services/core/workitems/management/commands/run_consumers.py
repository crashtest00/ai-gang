"""
Starts the Streams consumers (the command channel, webhook validation and,
from v5.2, the outbound Jira writer's own event-stream group) for every
configured project, from v6.0 the A2A archive group's consumers
(``a2a_archive.py``: one per distinct gateway and agent channel stream), and
blocks until SIGTERM/SIGINT. Mirrors the consumer half of the Node service's
src/index.js `main()` — the HTTP API is served separately (Django's own
runserver/gunicorn against core.wsgi, see manage.py runserver /
Procfile-style deployment), and the outbox relay runs as its own process
(`python manage.py relay`) so it can be killed/restarted independently.
"""

from __future__ import annotations

import signal
import time

from django.core.management.base import BaseCommand

from workitems import registry
from workitems.a2a_archive import create_archive_consumers
from workitems.command_consumer import create_command_consumer
from workitems.jira_writer import create_writer_consumer
from workitems.redis_client import new_client
from workitems.webhook_consumer import create_webhook_consumer


class Command(BaseCommand):
    help = ('Run the Streams command-consumer, webhook-consumer and Jira-writer event consumer '
            'for every configured project, and the A2A archive consumers.')

    def add_arguments(self, parser):
        parser.add_argument('--consumer-id', default=None, help='Override WORKITEM_CONSUMER_ID for this process.')

    def handle(self, *args, **options):
        from django.conf import settings

        registry.load()
        consumer_name = options.get('consumer_id') or settings.WORKITEM_CONSUMER_ID

        consumers = []
        for project in registry.get_project_names():
            command_consumer = create_command_consumer(new_client, project, consumer_name=consumer_name)
            command_consumer.start()
            consumers.append(command_consumer)

            webhook_consumer = create_webhook_consumer(new_client, project, consumer_name=consumer_name)
            webhook_consumer.start()
            consumers.append(webhook_consumer)

            # canonical-delivery-state.md REQ-09, "Canonical events with a
            # Jira side effect": the writer's second input. Started for
            # every project regardless of mode — `handle_event_envelope`
            # reads the project's mode per event through
            # `write_gate.mode_of` and makes no Jira call for one that is
            # local, so nothing here branches on the mode either.
            writer_consumer = create_writer_consumer(new_client, project, consumer_name=consumer_name)
            writer_consumer.start()
            consumers.append(writer_consumer)

            self.stdout.write(self.style.SUCCESS(
                f'[core] consuming commands, webhooks and events for project "{project}"'
            ))

        # durable-a2a-message-record.md §4: the `core-a2a-archive` group, on
        # each project's gateway stream and its agents' channel streams. One
        # consumer per distinct stream name, enumerated across every project
        # at once, since two agents may share a channelSuffix.
        for archive_consumer in create_archive_consumers(new_client, consumer_name=consumer_name):
            archive_consumer.start()
            consumers.append(archive_consumer)
            self.stdout.write(self.style.SUCCESS(
                f'[core] archiving A2A messages from "{archive_consumer.stream}"'
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
            self.stdout.write('[core] shutting down')
            for consumer in consumers:
                consumer.stop()
