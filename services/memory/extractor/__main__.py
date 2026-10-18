"""The extractor's process: ``python -m extractor --spool <dir>``.

The spool is the extractor's whole interface (REQ-05; brief I-17: no
listening socket, no published port, no network). Its layout:

    requests/<name>/       a file to extract: ``content`` and ``meta.json``,
                           moved in whole by the dispatcher (a rename)
    work/<name>/           a request this extractor has claimed (a rename),
                           while its child process parses it
    results/<name>.json    the result, written whole (a rename), for the
                           dispatcher to read and delete

``meta.json`` carries ``originalFilename`` and ``timeLimitSeconds``, the
time limit REQ-06 requires, which the dispatcher's settings declare
(``MEMORY_EXTRACTION_TIME_LIMIT_SECONDS``).

Results (``outcome``):

    extracted   the text (possibly empty) and the decided format
    failed      a recorded failure with its reason: an undeclared format, a
                parser error, the time limit, or a crash of the child
    lost        this extractor stopped while the file was being parsed, and
                found it in ``work/`` when it started again. Not a verdict
                on the file: the dispatcher counts it as an attempt and
                tries again, up to its attempt limit (brief I-26)
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

PACKAGE_PARENT = Path(__file__).resolve().parent.parent

LOST_REASON = 'the extractor stopped while this file was being parsed'


def _write_result(spool: Path, name: str, result: dict) -> None:
    result = {**result, 'name': name}
    tmp = spool / 'results' / f'.{name}.tmp'
    tmp.write_text(json.dumps(result))
    os.replace(tmp, spool / 'results' / f'{name}.json')


def _limits(cpu_seconds: int, memory_bytes: int | None):
    def apply():
        # Kernel backstops for a hostile file. Exceeding the CPU limit
        # kills the child with SIGXCPU; exceeding the address-space limit
        # makes its allocations fail. Either way only the child is hurt.
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))
        if memory_bytes:
            resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    return apply


def _signal_name(number: int) -> str:
    try:
        return signal.Signals(number).name
    except ValueError:
        return f'signal {number}'


def process(spool: Path, name: str, *, cpu_limit: int | None, memory_limit_mb: int | None) -> dict:
    """Parse one claimed request in a child process and return its result."""
    work = spool / 'work' / name
    try:
        meta = json.loads((work / 'meta.json').read_text())
        time_limit = float(meta['timeLimitSeconds'])
    except (OSError, ValueError, KeyError) as exc:
        return {'outcome': 'failed', 'format': None, 'reason': f'malformed extraction request: {exc}'}
    cpu = int(cpu_limit if cpu_limit is not None else max(1, int(time_limit) + 1))
    memory = memory_limit_mb * 1024 * 1024 if memory_limit_mb else None
    child = subprocess.Popen(
        [sys.executable, '-m', 'extractor.child', str(work)],
        cwd=PACKAGE_PARENT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        preexec_fn=_limits(cpu, memory), start_new_session=True,
    )
    try:
        _, stderr = child.communicate(timeout=time_limit)
    except subprocess.TimeoutExpired:
        child.kill()
        child.communicate()
        return {'outcome': 'failed', 'format': None,
                'reason': f'time limit exceeded: extraction ran longer than {time_limit:g} seconds'}
    out = work / 'out.json'
    if child.returncode == 0 and out.exists():
        try:
            return json.loads(out.read_text())
        except ValueError as exc:
            return {'outcome': 'failed', 'format': None, 'reason': f'extractor crashed: unreadable result ({exc})'}
    tail = (stderr or b'').decode('utf-8', 'replace').strip().splitlines()[-3:]
    if child.returncode < 0:
        how = f'killed by {_signal_name(-child.returncode)}'
    else:
        how = f'exit status {child.returncode}'
    detail = f': {" | ".join(tail)}' if tail else ''
    return {'outcome': 'failed', 'format': None, 'reason': f'extractor crashed ({how}){detail}'[:2000]}


def recover(spool: Path) -> None:
    """A request still in ``work/`` at startup was being parsed when this
    process stopped. Report it as lost, so the dispatcher tries again."""
    for work in sorted((spool / 'work').iterdir()):
        if work.is_dir():
            _write_result(spool, work.name, {'outcome': 'lost', 'format': None, 'reason': LOST_REASON})
            shutil.rmtree(work, ignore_errors=True)


def claim_next(spool: Path) -> str | None:
    requests = spool / 'requests'
    pending = sorted((p for p in requests.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime)
    for request in pending:
        try:
            os.rename(request, spool / 'work' / request.name)
        except OSError:
            continue  # withdrawn by the dispatcher meanwhile
        return request.name
    return None


def serve(spool: Path, *, cpu_limit=None, memory_limit_mb=None, idle_seconds=0.2, max_seconds=None) -> None:
    for sub in ('requests', 'work', 'results', 'tmp'):
        (spool / sub).mkdir(parents=True, exist_ok=True)
    recover(spool)
    started = time.monotonic()
    while max_seconds is None or time.monotonic() - started < max_seconds:
        name = claim_next(spool)
        if name is None:
            time.sleep(idle_seconds)
            continue
        result = process(spool, name, cpu_limit=cpu_limit, memory_limit_mb=memory_limit_mb)
        _write_result(spool, name, result)
        shutil.rmtree(spool / 'work' / name, ignore_errors=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog='extractor', description=__doc__.splitlines()[0])
    parser.add_argument('--spool', required=True, type=Path)
    parser.add_argument('--memory-limit-mb', type=int, default=2048,
                        help="Address-space limit for each file's child process (0: none).")
    parser.add_argument('--cpu-limit-seconds', type=int, default=None,
                        help="CPU-time limit for each file's child process (default: the request's time limit).")
    parser.add_argument('--max-seconds', type=float, default=None, help='Stop after this long (tests).')
    args = parser.parse_args(argv)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    serve(args.spool, cpu_limit=args.cpu_limit_seconds, memory_limit_mb=args.memory_limit_mb or None,
          max_seconds=args.max_seconds)
    return 0


if __name__ == '__main__':
    sys.exit(main())
