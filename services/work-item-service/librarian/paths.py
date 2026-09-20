"""
Turning a request's ``destination_repo`` and ``requested_path`` into real
places on disk — librarian.md REQ-06's second and third failure reasons,
and the build brief's instruction to "apply the same path-component
discipline the ingress app applies (see ``artifacts/storage.py``)".

**Repository identity** (spec §6's open question 3, settled by the product
owner and orchestrator, September 20, 2026): a repository is one directory
name **directly under** ``settings.PROJECTS_ROOT``. There are no
per-repository named volumes and no registry lookup — the projects root is
mounted once, and ``destination_repo`` is a child of it. A name that is
not a directory there is REQ-06's "unknown destination repository".

**The discipline.** ``artifacts/storage.py`` never lets a client-supplied
string become a path component: it re-derives the path from a parsed
``uuid.UUID``. A repository name and a requested path cannot be re-derived
from anything — they are the request — so instead every one of them is
checked, and the check is containment of the *resolved* path, after
symlinks, inside the *resolved* root. That is what makes ``..``, an
absolute path, a NUL, and a symlink pointing out of the tree all the same
single failure rather than four separate string tests that each have to be
remembered.
"""

from __future__ import annotations

import posixpath
from pathlib import Path

from django.conf import settings

from . import failures
from .failures import DeliveryFailure

# A repository is one directory name, so none of these may appear in it.
_FORBIDDEN_IN_REPO_NAME = ('/', '\\', '\x00')


def projects_root() -> Path:
    """The mounted projects root, resolved. Read from settings on every
    call rather than cached at import, so a test (and an operator changing
    the mount) gets the value that is actually configured now — the same
    reason ``artifacts.storage`` makes ``location`` a plain property."""
    return Path(settings.PROJECTS_ROOT).resolve()


def resolve_repository(destination_repo) -> Path:
    """The absolute directory of a destination repository.

    Raises ``DeliveryFailure(UNKNOWN_DESTINATION_REPO)`` for a name that is
    not a single component, is not present under the projects root, is not
    a directory, or resolves (through a symlink) to somewhere outside it.
    """
    if not isinstance(destination_repo, str) or not destination_repo.strip():
        raise DeliveryFailure(failures.UNKNOWN_DESTINATION_REPO,
                              f'destination repository {destination_repo!r} is not a name')
    name = destination_repo.strip()
    if name in ('.', '..') or any(ch in name for ch in _FORBIDDEN_IN_REPO_NAME):
        raise DeliveryFailure(failures.UNKNOWN_DESTINATION_REPO,
                              f'destination repository {destination_repo!r} is not a single directory name')

    root = projects_root()
    candidate = (root / name).resolve()
    if candidate.parent != root or not candidate.is_dir():
        raise DeliveryFailure(failures.UNKNOWN_DESTINATION_REPO,
                              f'no repository named {name!r} is mounted under the projects root')
    return candidate


def resolve_requested_path(repository_dir: Path, requested_path) -> str:
    """The repository-relative path a request asks for, normalized.

    Returns a forward-slash relative path with no ``.`` or ``..``
    components. Raises ``DeliveryFailure(PATH_OUTSIDE_REPOSITORY)`` when
    the request names nothing, names a directory rather than a file, or
    names a place that is not inside this repository once resolved.

    The returned path is where the request *asked* for the file. Where it
    actually lands is ``delivery.py``'s answer (REQ-04), which is not
    necessarily this.
    """
    if not isinstance(requested_path, str) or not requested_path.strip():
        raise DeliveryFailure(failures.PATH_OUTSIDE_REPOSITORY,
                              f'requested path {requested_path!r} is empty')
    raw = requested_path.strip()
    if '\x00' in raw:
        raise DeliveryFailure(failures.PATH_OUTSIDE_REPOSITORY, 'requested path contains a NUL byte')
    if raw.startswith('/') or (len(raw) > 1 and raw[1] == ':'):
        raise DeliveryFailure(failures.PATH_OUTSIDE_REPOSITORY,
                              f'requested path {requested_path!r} is absolute; it must be relative to the repository')

    if raw.replace('\\', '/').endswith('/'):
        # Checked BEFORE normalization, which silently discards a trailing
        # slash: `designs/` names a directory, and a request for a
        # directory is not a request for a file inside the repository.
        raise DeliveryFailure(failures.PATH_OUTSIDE_REPOSITORY,
                              f'requested path {requested_path!r} names a directory, not a file')

    relative = posixpath.normpath(raw.replace('\\', '/'))
    if relative in ('.', '..') or relative.startswith('../'):
        raise DeliveryFailure(failures.PATH_OUTSIDE_REPOSITORY,
                              f'requested path {requested_path!r} does not resolve inside repository '
                              f'{repository_dir.name!r}')

    # Containment of the RESOLVED path: this is the check that catches a
    # symlinked directory inside the repository pointing back out of it,
    # which no amount of string normalization above would see.
    absolute = (repository_dir / relative).resolve()
    if absolute != repository_dir and repository_dir not in absolute.parents:
        raise DeliveryFailure(failures.PATH_OUTSIDE_REPOSITORY,
                              f'requested path {requested_path!r} resolves outside repository '
                              f'{repository_dir.name!r}')
    if absolute == repository_dir:
        raise DeliveryFailure(failures.PATH_OUTSIDE_REPOSITORY,
                              f'requested path {requested_path!r} names the repository itself, not a file in it')
    return relative


def absolute_path(repository_dir: Path, relative_path: str) -> Path:
    """The absolute path of a repository-relative path this module already
    validated. Kept here so nothing else in the app joins the two by
    hand."""
    return repository_dir / relative_path
