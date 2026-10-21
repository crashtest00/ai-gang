"""File-change events on the harness volume (REQ-03), through Linux inotify.

The events make the copy prompt; they are never what makes it correct. The
watcher adds one watch per directory, recursively, and a directory created
later is watched and scanned as it appears. Anything it cannot do — a watch
the kernel refuses because `fs.inotify.max_user_watches` is exhausted, a
queue overflow, a directory created and written before its watch was added
(the race the session pickup spike saw, §9.4) — falls back to the
reconciling pass, never to loss.

Uses the C library directly through ctypes, so Memory needs no inotify
package.
"""

import ctypes
import ctypes.util
import errno
import logging
import os
import select
import struct

log = logging.getLogger('memory.watcher')

IN_MODIFY = 0x00000002
IN_CLOSE_WRITE = 0x00000008
IN_MOVED_FROM = 0x00000040
IN_MOVED_TO = 0x00000080
IN_CREATE = 0x00000100
IN_DELETE = 0x00000200
IN_DELETE_SELF = 0x00000400
IN_Q_OVERFLOW = 0x00004000
IN_IGNORED = 0x00008000
IN_ISDIR = 0x40000000
IN_NONBLOCK = 0o4000
IN_CLOEXEC = 0o2000000

MASK = IN_MODIFY | IN_CLOSE_WRITE | IN_MOVED_FROM | IN_MOVED_TO | IN_CREATE | IN_DELETE | IN_DELETE_SELF
_EVENT = struct.Struct('iIII')


class Watcher:
    """Recursive inotify watcher over `root`.

    `poll(timeout)` returns `(paths, rescan)`: the changed paths (absolute;
    a new directory is reported as itself, for the caller to scan) and
    whether the caller should run a full pass because events were lost.
    `max_watches` caps how many watches this watcher adds, so the
    exhausted-limit fallback can be exercised without changing the host's
    limit.
    """

    def __init__(self, root, max_watches=None):
        self.root = os.fspath(root)
        self.max_watches = max_watches
        self.failed_watches = 0
        self._libc = ctypes.CDLL(ctypes.util.find_library('c') or 'libc.so.6', use_errno=True)
        self._fd = self._libc.inotify_init1(IN_NONBLOCK | IN_CLOEXEC)
        if self._fd < 0:
            raise OSError(ctypes.get_errno(), 'inotify_init1 failed')
        self._dirs = {}

    def close(self):
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1

    def start(self):
        self._watch_tree(self.root)
        return self

    def _add_watch(self, path):
        if self.max_watches is not None and len(self._dirs) >= self.max_watches:
            self._refused(path, errno.ENOSPC)
            return False
        wd = self._libc.inotify_add_watch(self._fd, os.fsencode(path), MASK)
        if wd < 0:
            self._refused(path, ctypes.get_errno())
            return False
        self._dirs[wd] = path
        return True

    def _refused(self, path, err):
        self.failed_watches += 1
        if self.failed_watches == 1 or self.failed_watches % 1000 == 0:
            log.warning('could not watch %s (%s); %d directories are left to the reconciling pass '
                        '— size fs.inotify.max_user_watches for the harness volume',
                        path, os.strerror(err), self.failed_watches)

    def _watch_tree(self, top):
        for dirpath, dirnames, _ in os.walk(top):
            dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
            self._add_watch(dirpath)

    def poll(self, timeout):
        paths, rescan = [], False
        ready, _, _ = select.select([self._fd], [], [], timeout)
        if not ready:
            return paths, rescan
        while True:
            try:
                buf = os.read(self._fd, 65536)
            except BlockingIOError:
                break
            if not buf:
                break
            offset = 0
            while offset + _EVENT.size <= len(buf):
                wd, mask, _cookie, length = _EVENT.unpack_from(buf, offset)
                name = buf[offset + _EVENT.size: offset + _EVENT.size + length].rstrip(b'\0')
                offset += _EVENT.size + length
                if mask & IN_Q_OVERFLOW:
                    rescan = True
                    continue
                if mask & IN_IGNORED:
                    self._dirs.pop(wd, None)
                    continue
                base = self._dirs.get(wd)
                if base is None:
                    continue
                path = os.path.join(base, os.fsdecode(name)) if name else base
                if mask & IN_ISDIR and mask & (IN_CREATE | IN_MOVED_TO):
                    # Watch it and everything already inside it, and hand it
                    # back so the caller scans what was written before the
                    # watch existed.
                    self._watch_tree(path)
                paths.append(path)
        return paths, rescan
