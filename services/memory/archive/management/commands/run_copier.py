"""`memory-copier`'s process (REQ-03, REQ-06; §4).

At startup: rebuild whatever the archive holds and the manifest does not
(§4), start watching the harness volume, and run the reconciling pass. Then
copy on file-change events, run the reconciling pass every
MEMORY_RECONCILE_INTERVAL_SECONDS, and sweep for sessions that have gone
quiet by the interval every MEMORY_QUIET_SWEEP_SECONDS.

Settings refuse an interval that is not below the quiet interval before
this ever runs (memory/settings.py).
"""

import logging
import time
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from archive.copier import Copier
from archive.watcher import Watcher

log = logging.getLogger('memory.copier')


class Command(BaseCommand):
    help = "Copy every agent session on the harness volume into Memory's archive."

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true',
                            help='Rebuild, run one reconciling pass and exit.')
        parser.add_argument('--max-seconds', type=float, default=None,
                            help='Stop after this long (tests).')

    def handle(self, *args, once=False, max_seconds=None, **options):
        copier = Copier()
        root = Path(settings.MEMORY_HARNESS_ROOT)
        copier.rebuild_from_archive()
        watcher = None
        if not once and settings.MEMORY_WATCHER_ENABLED:
            try:
                watcher = Watcher(root, max_watches=settings.MEMORY_MAX_WATCHES).start()
            except OSError as exc:
                log.warning('file-change events unavailable (%s); copying on the reconciling pass only', exc)
        elif not once:
            log.info('watcher disabled (MEMORY_WATCHER_ENABLED); copying on the reconciling pass only')
        copier.reconcile()
        if once:
            return
        started = time.monotonic()
        next_pass = started + settings.MEMORY_RECONCILE_INTERVAL_SECONDS
        next_sweep = started + settings.MEMORY_QUIET_SWEEP_SECONDS
        try:
            while max_seconds is None or time.monotonic() - started < max_seconds:
                now = time.monotonic()
                timeout = max(0.0, min(next_pass, next_sweep) - now)
                if watcher is not None:
                    paths, rescan = watcher.poll(min(timeout, 1.0))
                    if paths:
                        # Let a burst of writes settle, then copy each path once.
                        time.sleep(settings.MEMORY_EVENT_SETTLE_SECONDS)
                        more, more_rescan = watcher.poll(0)
                        paths += more
                        rescan = rescan or more_rescan
                    for path in dict.fromkeys(paths):
                        try:
                            parts = Path(path).relative_to(root).parts
                        except ValueError:
                            continue
                        copier.sync_tree(parts)
                    if rescan:
                        log.warning('inotify queue overflowed; running the reconciling pass')
                        copier.reconcile()
                else:
                    time.sleep(min(timeout, 1.0))
                now = time.monotonic()
                if now >= next_pass:
                    copier.reconcile()
                    next_pass = now + settings.MEMORY_RECONCILE_INTERVAL_SECONDS
                    next_sweep = now + settings.MEMORY_QUIET_SWEEP_SECONDS
                elif now >= next_sweep:
                    copier.sweep_quiet()
                    next_sweep = now + settings.MEMORY_QUIET_SWEEP_SECONDS
        finally:
            if watcher is not None:
                watcher.close()
