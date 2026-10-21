"""``memory-indexer``'s process (Corpus Indexing REQ-04).

On start it asks every Core enumeration for a catch-up and indexes the
platform corpus if the checkout's commit is new. That is how an indexer
stopped for longer than the streams keep their entries recovers (§4). Then
it loops:

- joins any new work-item event stream (every
  MEMORY_INDEX_STREAM_DISCOVERY_SECONDS);
- reads and acts on events (``index.events``);
- runs each catch-up an event asked for (``index.follow.catch_up_core``);
- follows the archive's session listing and notes read (every
  MEMORY_INDEX_ARCHIVE_FOLLOW_SECONDS) and the extraction record (every
  MEMORY_INDEX_LIBRARY_FOLLOW_SECONDS);
- retries recorded failures (every MEMORY_INDEX_RETRY_SECONDS, REQ-09).

``--once`` runs each of those once, after draining every event, and exits.
"""

import logging
import socket
import time

import redis
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import close_old_connections
from django.utils import timezone

from index import follow, reads, store
from index.events import EventConsumer
from index.models import IndexCursor, IndexState
from index.status import index_status

log = logging.getLogger('memory.index')


def _heartbeat():
    IndexState.objects.get_or_create(pk=1)
    IndexState.objects.filter(pk=1).update(heartbeat_at=timezone.now())


def _attempt(what, step):
    try:
        return step()
    except reads.SourceUnavailable as err:
        log.warning('%s: owner unavailable (%s); tried again on the next pass', what, err)
    return 0


def _core_catch_ups():
    done = 0
    for name in reads.CORE_ENUMERATIONS:
        row = IndexCursor.objects.filter(name=name).first()
        if row is None or not row.caught_up or row.requested > row.completed:
            done += _attempt(name, lambda n=name: follow.catch_up_core(n)) or 0
    return done


class Command(BaseCommand):
    help = 'Keep the corpus index current (corpus-indexing.md).'

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true', help='One full pass, then exit.')
        parser.add_argument('--max-seconds', type=float, default=None, help='Stop after this long.')

    def handle(self, *args, once=False, max_seconds=None, **options):
        consumer = EventConsumer(settings.MEMORY_REDIS_URL, settings.MEMORY_INDEX_CONSUMER or socket.gethostname())
        for name in reads.CORE_ENUMERATIONS:
            store.request_catch_up(name)
        with store.index_lock():
            follow.follow_platform()
        due = {'discover': 0.0, 'archive': 0.0, 'library': 0.0, 'retry': time.monotonic(), 'status': 0.0}
        started, idle = time.monotonic(), False
        log.info('memory-indexer started (consumer %s)', consumer.consumer)
        while max_seconds is None or time.monotonic() - started < max_seconds:
            close_old_connections()
            _heartbeat()
            now = time.monotonic()
            handled = 0
            try:
                if once or now >= due['discover']:
                    consumer.discover()
                    due['discover'] = now + settings.MEMORY_INDEX_STREAM_DISCOVERY_SECONDS
                while True:
                    count = consumer.poll(block_ms=0 if once else (500 if idle else 0))
                    handled += count
                    if not once or count == 0:
                        break
            except redis.RedisError as err:
                log.warning('event streams unavailable (%s); the followed reads continue', err)
                time.sleep(1)
            with store.index_lock():
                work = _core_catch_ups()
                if once or now >= due['archive']:
                    work += _attempt('sessions', follow.follow_sessions) + _attempt('notes', follow.follow_notes)
                    due['archive'] = now + settings.MEMORY_INDEX_ARCHIVE_FOLLOW_SECONDS
                if once or now >= due['library']:
                    work += follow.follow_library()
                    due['library'] = now + settings.MEMORY_INDEX_LIBRARY_FOLLOW_SECONDS
                if once or now >= due['retry']:
                    _attempt('retry', follow.retry_failures)
                    due['retry'] = now + settings.MEMORY_INDEX_RETRY_SECONDS
            if now >= due['status']:
                status = index_status(consumer.client)
                log.info('index %s: %s', 'incomplete' if status['incomplete'] else 'current',
                         '; '.join(status['reasons']) or 'no lag')
                due['status'] = now + 60
            if once:
                return
            idle = handled == 0 and not work
