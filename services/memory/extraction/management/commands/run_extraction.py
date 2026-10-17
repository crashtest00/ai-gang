"""``memory-extraction``'s process: the dispatcher (artifact-text-extraction.md).

Consumes ``artifact.uploaded`` events into jobs (``extraction.events``),
runs the reconciliation sweep at startup and every
MEMORY_EXTRACTION_SWEEP_SECONDS in a thread of its own
(``extraction.sweep``), and works the job store one job at a time
(``extraction.jobs``), handing each file to the extractor process through
the spool (REQ-05). It never parses a file itself, so a file that crashes
the extractor cannot terminate it.

On start it first resumes any job a previous run of this worker left
``running`` (REQ-07).
"""

import logging
import threading
import time

import redis
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection

from extraction import core_client, jobs
from extraction.events import EventConsumer
from extraction.spool import Spool
from extraction.sweep import sweep

log = logging.getLogger('memory.extraction')


def _sweep_forever(stop: threading.Event) -> None:
    try:
        while not stop.is_set():
            try:
                sweep()
            except core_client.CoreUnavailable as err:
                log.warning('extraction sweep could not read Core: %s', err)
            except Exception:  # noqa: BLE001 - one failed pass must not end the sweeps
                log.exception('extraction sweep failed')
            stop.wait(settings.MEMORY_EXTRACTION_SWEEP_SECONDS)
    finally:
        connection.close()


class Command(BaseCommand):
    help = 'Extract text from artifact-library files (the dispatcher; the parsing is the extractor process).'

    def add_arguments(self, parser):
        parser.add_argument('--max-seconds', type=float, default=None, help='Stop after this long (tests).')

    def handle(self, *args, max_seconds=None, **options):
        worker = jobs.worker_id()
        spool = Spool(settings.MEMORY_EXTRACTION_SPOOL)
        consumer = EventConsumer(settings.MEMORY_REDIS_URL, worker)
        stop = threading.Event()
        sweeper = threading.Thread(target=_sweep_forever, args=(stop,), name='extraction-sweep', daemon=True)
        sweeper.start()
        log.info('memory-extraction %s started: spool %s, time limit %gs, attempt limit %d, sweep every %gs',
                 worker, spool.root, settings.MEMORY_EXTRACTION_TIME_LIMIT_SECONDS,
                 settings.MEMORY_EXTRACTION_ATTEMPT_LIMIT, settings.MEMORY_EXTRACTION_SWEEP_SECONDS)
        stale_after = 4 * (settings.MEMORY_EXTRACTION_TIME_LIMIT_SECONDS + settings.MEMORY_EXTRACTION_GRACE_SECONDS)
        next_purge = 0.0
        started = time.monotonic()
        idle = False
        try:
            while max_seconds is None or time.monotonic() - started < max_seconds:
                try:
                    consumer.poll(block_ms=500 if idle else 0)
                except redis.RedisError as err:
                    log.warning('artifact events unavailable (%s); jobs and the sweep continue', err)
                    time.sleep(1)
                job = jobs.claim(worker)
                idle = job is None
                if job is not None:
                    state = jobs.run(job, spool)
                    log.info('extraction job %s for %s: %s (%s)', job.id, job.artifact_id, state, job.note)
                if time.monotonic() >= next_purge:
                    spool.purge_stale(stale_after)
                    next_purge = time.monotonic() + stale_after
        finally:
            stop.set()
