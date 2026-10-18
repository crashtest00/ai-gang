"""The dispatcher's side of the spool, the extractor's only interface (REQ-05).

The layout and the result outcomes are the extractor's
(``services/memory/extractor/__main__.py``). A request is staged under
``tmp/``, then moved into ``requests/`` whole by one rename, so the
extractor never sees half a file. The extractor claims it by moving it to
``work/`` and answers in ``results/<name>.json``.

``await_result`` waits for the extractor to claim the request and then for
its result, and tells the caller which of three things happened:

    a result     the extractor's verdict on the file (``extracted``,
                 ``failed``, or ``lost`` when it stopped mid-file)
    UNCLAIMED    the extractor never picked the request up within the grace
                 period; the request is withdrawn. Says nothing about the
                 file: the extractor is down or busy
    NO_RESULT    claimed, but no result within the time limit plus grace;
                 the extractor stopped or hung mid-file
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

UNCLAIMED = 'unclaimed'
NO_RESULT = 'no-result'
POLL_SECONDS = 0.05


class Spool:
    def __init__(self, root):
        self.root = Path(root)
        for sub in ('requests', 'work', 'results', 'tmp'):
            (self.root / sub).mkdir(parents=True, exist_ok=True)

    def clear(self, job_id: int) -> None:
        """Drop anything an earlier attempt of this job left behind, except a
        request the extractor is parsing now (``work/``, the extractor's)."""
        prefix = f'{job_id}-'
        for sub in ('tmp', 'requests', 'results'):
            for entry in (self.root / sub).glob(f'{prefix}*'):
                if entry.is_dir():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    entry.unlink(missing_ok=True)

    def purge_stale(self, max_age: float) -> None:
        """Delete results nobody will read: an attempt's result that arrived
        after its worker had given up on it, or stopped."""
        cutoff = time.time() - max_age
        for entry in (self.root / 'results').glob('*.json'):
            try:
                if entry.stat().st_mtime < cutoff:
                    entry.unlink(missing_ok=True)
            except OSError:
                continue

    def stage(self, name: str) -> Path:
        staged = self.root / 'tmp' / name
        shutil.rmtree(staged, ignore_errors=True)
        staged.mkdir()
        return staged

    def discard(self, name: str) -> None:
        shutil.rmtree(self.root / 'tmp' / name, ignore_errors=True)

    def submit(self, name: str, *, original_filename: str, time_limit: float) -> None:
        staged = self.root / 'tmp' / name
        (staged / 'meta.json').write_text(json.dumps(
            {'originalFilename': original_filename, 'timeLimitSeconds': time_limit}))
        os.rename(staged, self.root / 'requests' / name)

    def _withdraw(self, name: str) -> bool:
        try:
            os.rename(self.root / 'requests' / name, self.root / 'tmp' / name)
        except OSError:
            return False  # claimed meanwhile
        shutil.rmtree(self.root / 'tmp' / name, ignore_errors=True)
        return True

    def _take_result(self, name: str):
        path = self.root / 'results' / f'{name}.json'
        if not path.exists():
            return None
        result = json.loads(path.read_text())
        path.unlink(missing_ok=True)
        return result

    def await_result(self, name: str, *, time_limit: float, grace: float):
        claim_deadline = time.monotonic() + grace
        while (self.root / 'requests' / name).exists():
            if time.monotonic() >= claim_deadline and self._withdraw(name):
                return UNCLAIMED
            time.sleep(POLL_SECONDS)
        deadline = time.monotonic() + time_limit + grace
        while True:
            result = self._take_result(name)
            if result is not None:
                return result
            if time.monotonic() >= deadline:
                return NO_RESULT
            time.sleep(POLL_SECONDS)
