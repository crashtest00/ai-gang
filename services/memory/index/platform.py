"""AI Gang's own specification corpus, the `platform` scope (Corpus
Indexing REQ-08): the Markdown under `docs/` and `setup/` in the
deployment's own Source checkout, mounted read-only into memory-indexer at
MEMORY_PLATFORM_CORPUS_ROOT.

It is read from the checkout's repository at its current commit (``git
ls-tree`` and ``git cat-file``), not from the working tree. Each unit's
reference names that commit, so a passage resolves to the exact text it was
cut from even after a ``git pull`` moves the checkout (build brief I-23):
``git show <commit>:<path>``. Reading the commit also limits the corpus to
tracked files, so a dependency's README under an ignored directory (for
example `setup/graphs/engine/node_modules/`) is never platform corpus.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOTS = ('docs', 'setup')
SUFFIX = '.md'
TIMEOUT_SECONDS = 60


class PlatformUnavailable(Exception):
    """The checkout has no readable commit, so no unit could name one."""


def _git(root: str, *args: str) -> bytes:
    # The checkout is mounted read-only and owned by another uid, so git's
    # ownership check is waived for this command only. Nothing here writes.
    try:
        done = subprocess.run(['git', '-c', 'safe.directory=*', '-C', root, *args], capture_output=True,
                              timeout=TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.TimeoutExpired) as err:
        raise PlatformUnavailable(f'git {args[0]} in {root}: {err}') from err
    if done.returncode != 0:
        raise PlatformUnavailable(
            f'git {args[0]} in {root}: {done.stderr.decode("utf-8", "replace").strip() or done.returncode}')
    return done.stdout


def head_commit(root: str) -> str:
    # git looks upward for a repository; the corpus is this checkout's only.
    top = _git(root, 'rev-parse', '--show-toplevel').decode().strip()
    if Path(top).resolve() != Path(root).resolve():
        raise PlatformUnavailable(f'{root} is not the top of a git checkout (git found {top})')
    return _git(root, 'rev-parse', '--verify', 'HEAD^{commit}').decode().strip()


def documents(root: str, commit: str) -> list[tuple[str, str]]:
    """``(path, text)`` for every Markdown file under the corpus roots at
    ``commit``, in path order."""
    listed = _git(root, 'ls-tree', '-r', '-z', '--name-only', commit, '--', *ROOTS)
    paths = sorted(p for p in listed.decode('utf-8', 'replace').split('\0') if p.endswith(SUFFIX))
    return [(path, _git(root, 'cat-file', 'blob', f'{commit}:{path}').decode('utf-8', 'replace')) for path in paths]
