"""
Turning a request's ``destination_repo`` and ``requested_path`` into real
places on disk — librarian.md REQ-06's second and third failure reasons,
and the build brief's instruction to "apply the same path-component
discipline the ingress app applies (see ``artifacts/storage.py``)".

**Repository identity** (spec §6's open question 3, settled by the product
owner and orchestrator, September 20, 2026): a repository is named by one
directory name **directly under** ``settings.PROJECTS_ROOT``. There are no
per-repository named volumes and no registry lookup — the projects root is
mounted once, and ``destination_repo`` is a child of it. A name that is
not a directory there is REQ-06's "unknown destination repository".

**The repository is the project's ``src/``, not the project directory.**
``scripts/init-project.sh`` bind-mounts ``projects/<name>/src`` into the
project's container as ``/workspace``, and ``projects/<name>/src/.git`` is
the git root. The project directory above it holds the project's own
``docker-compose.yml``, ``Dockerfile`` and ``.env`` — deployment
scaffolding, not repository content. Delivery "writes into the
repository's working tree" (librarian.md §2) and answers with the path the
artifact actually occupies (REQ-04), so the directory this module resolves
to, and every path answered relative to it, is
``<PROJECTS_ROOT>/<name>/<settings.PROJECTS_REPO_SUBDIR>``: simultaneously
the git working tree and the requesting agent's ``/workspace``. A project
directory with no such subdirectory is not a repository this librarian can
deliver into, and is answered ``unknown_destination_repo`` rather than
written beside the scaffolding.

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
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings

from . import content, failures
from .failures import DeliveryFailure

# A repository is one directory name, so none of these may appear in it.
_FORBIDDEN_IN_REPO_NAME = ('/', '\\', '\x00')


@dataclass(frozen=True)
class Repository:
    """One destination repository: the name a request called it by, and
    the working tree that name resolves to.

    Both are needed and neither substitutes for the other. ``name`` is
    what the delivery record and the advisory lock key on — it is the
    project, and it stays ``hello-web`` rather than becoming ``src`` for
    every project on the machine. ``directory`` is where files are read,
    searched and written, and every path in an answer is relative to it.
    """

    name: str
    directory: Path


def projects_root() -> Path:
    """The mounted projects root, resolved. Read from settings on every
    call rather than cached at import, so a test (and an operator changing
    the mount) gets the value that is actually configured now — the same
    reason ``artifacts.storage`` makes ``location`` a plain property."""
    return Path(settings.PROJECTS_ROOT).resolve()


def resolve_repository(destination_repo) -> Repository:
    """The destination repository a request names.

    Raises ``DeliveryFailure(UNKNOWN_DESTINATION_REPO)`` for a name that is
    not a single component, is not present under the projects root, is not
    a directory, resolves (through a symlink) to somewhere outside it, or
    holds no ``settings.PROJECTS_REPO_SUBDIR`` working tree.
    """
    if not isinstance(destination_repo, str) or not destination_repo.strip():
        raise DeliveryFailure(failures.UNKNOWN_DESTINATION_REPO,
                              f'destination repository {destination_repo!r} is not a name')
    name = destination_repo.strip()
    if name in ('.', '..') or any(ch in name for ch in _FORBIDDEN_IN_REPO_NAME):
        raise DeliveryFailure(failures.UNKNOWN_DESTINATION_REPO,
                              f'destination repository {destination_repo!r} is not a single directory name')

    root = projects_root()
    project_dir = (root / name).resolve()
    if project_dir.parent != root or not project_dir.is_dir():
        raise DeliveryFailure(failures.UNKNOWN_DESTINATION_REPO,
                              f'no repository named {name!r} is mounted under the projects root')

    subdir = settings.PROJECTS_REPO_SUBDIR
    working_tree = (project_dir / subdir).resolve()
    if working_tree.parent != project_dir or not working_tree.is_dir():
        raise DeliveryFailure(
            failures.UNKNOWN_DESTINATION_REPO,
            f'project {name!r} has no {subdir!r} working tree; the librarian delivers into '
            f'{name}/{subdir}, never into the project directory that holds its compose file',
        )
    return Repository(name=name, directory=working_tree)


def resolve_requested_path(repository: Repository, requested_path) -> str:
    """The repository-relative path a request asks for, normalized.

    Returns a forward-slash relative path with no ``.`` or ``..``
    components, relative to the repository's working tree — which is the
    requesting agent's ``/workspace``, so what a request asks for and what
    it is answered are in the frame the agent already works in.

    Raises ``DeliveryFailure(PATH_OUTSIDE_REPOSITORY)`` when the request
    names nothing, names a directory rather than a file, names a place the
    content search would never look (``.git``, ``node_modules``), or names
    a place that is not inside this repository once resolved.

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
                              f'{repository.name!r}')

    # The write rule and the search rule are one list. ``content.py`` prunes
    # these names at EVERY depth of its walk (not just the top), so a
    # delivery under one of them anywhere in the path — not only as the
    # first component — could never be found again by REQ-03's content
    # search; ``.git`` in particular is the repository's own object store
    # (including a submodule's, nested arbitrarily deep), which a delivery
    # has no business writing into.
    skipped_component = next(
        (component for component in relative.split('/') if component in content.SKIPPED_DIRECTORIES),
        None,
    )
    if skipped_component is not None:
        raise DeliveryFailure(failures.PATH_OUTSIDE_REPOSITORY,
                              f'requested path {requested_path!r} is under {skipped_component!r}, which is not '
                              f'repository content the librarian delivers into')

    # Containment of the RESOLVED path: this is the check that catches a
    # symlinked directory inside the repository pointing back out of it,
    # which no amount of string normalization above would see. It is a
    # check at one moment; ``delivery.py`` re-checks the real path after
    # the create, because a symlink can be planted after this returns.
    absolute = (repository.directory / relative).resolve()
    if absolute != repository.directory and repository.directory not in absolute.parents:
        raise DeliveryFailure(failures.PATH_OUTSIDE_REPOSITORY,
                              f'requested path {requested_path!r} resolves outside repository '
                              f'{repository.name!r}')
    if absolute == repository.directory:
        raise DeliveryFailure(failures.PATH_OUTSIDE_REPOSITORY,
                              f'requested path {requested_path!r} names the repository itself, not a file in it')
    return relative


def absolute_path(repository_dir: Path, relative_path: str) -> Path:
    """The absolute path of a repository-relative path this module already
    validated. Kept here so nothing else in the app joins the two by
    hand."""
    return repository_dir / relative_path
