"""
Finding an artifact that is already in a repository — librarian.md REQ-03
step 4, and §4's "Matching an artifact already in a repository".

"REQ-03's search compares content rather than filename, since a delivered
file may have been renamed and an unrelated file may share a name. The
digest used is settled during implementation." (spec §6's open question 2:
"any collision-resistant digest satisfies REQ-03's search".)

**sha256.** Collision-resistant in the sense the requirement needs — two
different files that both answer "this is the artifact" would need a
deliberate collision against a 256-bit hash — and in the standard library,
so it adds no dependency.

**Size first.** A file whose size differs from the artifact's cannot be a
byte-for-byte copy of it, and ``stat`` is far cheaper than reading. The
search therefore hashes only same-sized candidates, which on a real
repository is a small handful.

**What is skipped**, and why each:

- ``.git`` — the repository's own object store. A delivered file's blob
  lives there byte-for-byte after a commit, so searching it would "find"
  the artifact at a path inside ``.git`` and answer with it. It is also
  never where a building agent wants a file.
- ``node_modules`` — vendored dependencies, not repository content; large
  enough to dominate the walk, and a match there is a package's own file
  that happens to be identical, never a delivery.
- **Symlinks**, both to files and to directories. The search never follows
  one, so it cannot report a path that leaves the repository, or loop.

Nothing else is skipped: a repository's own build output or vendored
assets are searched, because a file delivered there is still a delivery.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Optional

DIGEST_NAME = 'sha256'
_CHUNK = 1024 * 1024

# Directory names never descended into. See the module docstring.
SKIPPED_DIRECTORIES = frozenset({'.git', 'node_modules'})


def digest_file(path: Path) -> str:
    """The artifact's digest, over the file's *current* bytes. An artifact
    edited in place on the volume (ingress REQ-02) therefore matches what
    it now holds, not what it held when it was uploaded."""
    digest = hashlib.new(DIGEST_NAME)
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b''):
            digest.update(chunk)
    return digest.hexdigest()


def find_by_content(repository_dir: Path, *, size: int, digest: str) -> Optional[str]:
    """The repository-relative path of the first file in ``repository_dir``
    whose bytes are the artifact's, or ``None``.

    Deterministic: the walk is sorted at every level and the first match in
    that order wins, so two librarians searching the same repository return
    the same path rather than whichever the filesystem happened to list
    first.
    """
    for dirpath, dirnames, filenames in os.walk(repository_dir, followlinks=False):
        dirnames[:] = sorted(
            name for name in dirnames
            if name not in SKIPPED_DIRECTORIES and not os.path.islink(os.path.join(dirpath, name))
        )
        for name in sorted(filenames):
            candidate = Path(dirpath) / name
            if candidate.is_symlink():
                continue
            try:
                if candidate.stat().st_size != size:
                    continue
                if digest_file(candidate) != digest:
                    continue
            except OSError:
                # A file that vanished or cannot be read mid-walk is not a
                # match; the walk is a search, not an audit.
                continue
            return candidate.relative_to(repository_dir).as_posix()
    return None
