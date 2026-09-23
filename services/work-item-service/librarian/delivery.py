"""
The resolution order, the copy, and the record — librarian.md REQ-02,
REQ-03, REQ-04, REQ-05 and REQ-07.

REQ-03, verbatim, is the shape of ``_resolve`` below:

    1. Is there a delivery record for this artifact and repository?
    2. If so, at what path was it delivered?
    3. Is the artifact still at that path? If yes, answer with that path.
    4. If not, search the destination repository for the artifact by
       content. If found, answer with the path it was found at and correct
       the record.
    5. If there is no record, or the artifact is not in the repository,
       copy it to ``requested_path`` and answer with the resulting path.

Step 3 is an **existence check**, not a content comparison: a file that a
human edited after delivery is still the delivered file, and re-delivering
over their edit would be the opposite of what REQ-03 asks. (Recorded in
``SHOVEL_READY_AUDIT.md`` under "Withdrawn in this pass", so it is not
rediscovered as a defect.)

Step 4 runs whenever steps 1-3 did not answer — including when there is no
record at all. Step 5's "or the artifact is not in the repository" is a
fact about the repository, and the content search is the only thing that
establishes it; skipping the search on a missing record would let a
repository that already holds the artifact (delivered before the record
was lost, or put there by hand) receive a second copy at a second path,
which is exactly what REQ-02 forbids.
"""

from __future__ import annotations

import hashlib
import os
import posixpath
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone

from artifacts.models import Artifact

from . import content, failures, paths
from .envelope import read_field
from .failures import DeliveryFailure
from .models import ArtifactDelivery

# What the librarian did, reported on every confirmation (REQ-04's "what
# happened, not what was asked").
ACTION_COPIED = 'copied'            # step 5 — the bytes were written now
ACTION_ALREADY_PRESENT = 'already_present'   # step 3, or step 4 with no prior record
ACTION_RELOCATED = 'relocated'      # step 4 — found elsewhere, record corrected

# How many times a name collision is stepped past before giving up. A
# repository holding 200 unrelated files named `mockup.png`, `mockup-1.png`
# ... `mockup-199.png` is a request to answer with a failure, not to keep
# counting.
MAX_COLLISION_ATTEMPTS = 200


@dataclass(frozen=True)
class DeliveryRequest:
    """REQ-01's field contract, parsed. ``task_id`` is the only optional
    one."""

    requested_by: str
    artifact_id: str
    destination_repo: str
    requested_path: str
    task_id: Optional[str] = None


# The order fields are checked, which is the order REQ-01 names them.
_REQUIRED_FIELDS = (
    ('requestedBy', 'requested_by'),
    ('artifactId', 'artifact_id'),
    ('destinationRepo', 'destination_repo'),
    ('requestedPath', 'requested_path'),
)


def parse_request(payload: dict[str, Any]) -> DeliveryRequest:
    """REQ-01's field contract. Raises
    ``DeliveryFailure(MISSING_FIELD)`` naming the first missing field, per
    REQ-01's acceptance ("a request missing a required field is answered
    with a failure naming the missing field")."""
    values = {}
    for camel, snake in _REQUIRED_FIELDS:
        value = read_field(payload, camel, snake)
        if value is None or (isinstance(value, str) and not value.strip()):
            raise DeliveryFailure(failures.MISSING_FIELD, f'request is missing required field "{snake}"')
        values[snake] = str(value).strip()

    task_id = read_field(payload, 'taskId', 'task_id')
    return DeliveryRequest(task_id=str(task_id) if task_id is not None else None, **values)


def deliver(request: DeliveryRequest) -> dict[str, Any]:
    """Resolve one request and return the confirmation's payload fields.

    Raises ``DeliveryFailure`` for every REQ-06 reason; the caller
    (``consumer.py``) turns either outcome into exactly one response.

    Everything from the lock onwards runs inside one transaction, so the
    record is written synchronously as part of handling the request
    (REQ-05) and the advisory lock is released by the same commit that
    makes the record visible.
    """
    artifact = _artifact_or_failure(request.artifact_id)
    repository = paths.resolve_repository(request.destination_repo)
    requested = paths.resolve_requested_path(repository, request.requested_path)
    source = _source_file(artifact)

    with transaction.atomic():
        _lock(str(artifact.id), repository.name)
        _test_only_hold_delay()
        return _resolve(request, artifact, repository, requested, source)


# --- REQ-07: one copy, whatever the arrival order ------------------------

def advisory_lock_key(artifact_id: str, repository: str) -> int:
    """The Postgres advisory-lock key for one (artifact, repository) pair.

    A hash rather than a row id because REQ-07's serialization has to hold
    **before** the delivery record exists — "so that correctness does not
    depend on the delivery record having been written first" — and there is
    therefore nothing in the database to lock. Exported because the
    concurrency test asserts against the same key the production path uses,
    rather than against a key it derived for itself.
    """
    digest = hashlib.sha256(f'{artifact_id}\x00{repository}'.encode()).digest()
    return int.from_bytes(digest[:8], 'big', signed=True)


def _lock(artifact_id: str, repository: str) -> None:
    """``pg_advisory_xact_lock`` — held until this transaction commits or
    rolls back, so no path can leak it, and honoured across processes and
    machines, so two librarian containers serialize against each other and
    not merely two threads in one."""
    with connection.cursor() as cursor:
        cursor.execute('SELECT pg_advisory_xact_lock(%s)', [advisory_lock_key(artifact_id, repository)])


def _test_only_hold_delay() -> None:
    """Sleep inside the lock, so a test can make two requests genuinely
    overlap instead of hoping they do. Zero in every deployment; the same
    test-only-knob shape as ``RELAY_ROW_DELAY_MS`` in
    ``workitems/relay.py``."""
    delay_ms = getattr(settings, 'LIBRARIAN_LOCK_HOLD_DELAY_MS', 0)
    if delay_ms:
        time.sleep(delay_ms / 1000)


# --- REQ-03's five steps -------------------------------------------------

def _resolve(request: DeliveryRequest, artifact, repository: paths.Repository, requested: str,
             source: Path) -> dict[str, Any]:
    # Every path below — the record's, the search's and the copy's — is
    # relative to the repository's working tree, which is the requesting
    # agent's /workspace (paths.py).
    repository_dir = repository.directory
    record = ArtifactDelivery.objects.filter(artifact_id=artifact.id, repository=repository.name).first()

    # Steps 1-3. An existence check, deliberately: see the module docstring.
    if record is not None and paths.absolute_path(repository_dir, record.path).is_file():
        return _confirmation(record.path, ACTION_ALREADY_PRESENT, record)

    # Step 4. The filesystem is authoritative (REQ-05), so what it says is
    # in the repository outranks what the record said.
    try:
        found = content.find_by_content(
            repository_dir, size=source.stat().st_size, digest=content.digest_file(source),
        )
    except OSError as err:
        raise DeliveryFailure(failures.COPY_FAILED,
                              f'artifact {artifact.id} could not be read from the artifact volume: {err}') from err
    if found is not None:
        action = ACTION_RELOCATED if record is not None else ACTION_ALREADY_PRESENT
        return _confirmation(found, action, _record(request, artifact, repository.name, found))

    # Step 5.
    delivered = _copy_into(source, repository_dir, requested)
    return _confirmation(delivered, ACTION_COPIED, _record(request, artifact, repository.name, delivered))


def _copy_into(source: Path, repository_dir: Path, requested: str) -> str:
    """Copy the artifact in and return the repository-relative path it
    actually occupies — read back from the file this created, never echoed
    from the request (REQ-04)."""
    destination_parent = paths.absolute_path(repository_dir, requested).parent
    # Recorded BEFORE mkdir, since afterwards every one of these exists and
    # the refusal path below needs to know which of them mkdir actually
    # created for this delivery, as opposed to directories that were
    # already there.
    created_directories = _missing_ancestors(destination_parent, repository_dir)
    try:
        destination_parent.mkdir(parents=True, exist_ok=True)
    except OSError as err:
        raise DeliveryFailure(failures.COPY_FAILED,
                              f'could not create directory for {requested!r}: {err}') from err

    for candidate in _collision_candidates(repository_dir, requested):
        destination = paths.absolute_path(repository_dir, candidate)
        try:
            _create_atomically(source, destination)
        except FileExistsError:
            # Something outside this librarian took the name between the
            # existence check and the create. Step past it and try the next.
            continue
        except OSError as err:
            raise DeliveryFailure(failures.COPY_FAILED,
                                  f'copy of the artifact to {candidate!r} failed: {err}') from err

        # REQ-04's read-back: the answer is what is on disk now.
        try:
            written = destination.stat().st_size
        except OSError as err:
            raise DeliveryFailure(failures.COPY_FAILED,
                                  f'copy to {candidate!r} left nothing readable: {err}') from err
        if written != source.stat().st_size:
            raise DeliveryFailure(failures.COPY_FAILED,
                                  f'copy to {candidate!r} is {written} bytes, the artifact is '
                                  f'{source.stat().st_size}')
        try:
            _refuse_a_file_that_left_the_repository(repository_dir, destination, candidate)
        except DeliveryFailure:
            _remove_directories_this_delivery_created(created_directories)
            raise
        return candidate

    raise DeliveryFailure(failures.COPY_FAILED,
                          f'{requested!r} and {MAX_COLLISION_ATTEMPTS} adjusted names after it are all taken in '
                          f'this repository')


def _missing_ancestors(path: Path, boundary: Path) -> list[Path]:
    """Every prefix of ``path``, innermost first, that does not exist yet —
    exactly the directories ``path.mkdir(parents=True)`` is about to create
    as a side effect. Stops at ``boundary`` (the repository's working tree,
    which ``resolve_repository`` already guarantees exists), so the walk
    always terminates even when a planted symlink makes an intermediate
    component "exist" by pointing somewhere else entirely — ``exists()``
    follows it exactly as ``mkdir`` itself would, so what is reported
    missing here is what mkdir will actually create."""
    missing = []
    current = path
    while current != boundary and not current.exists():
        missing.append(current)
        current = current.parent
    return missing


def _remove_directories_this_delivery_created(directories: list[Path]) -> None:
    """``rmdir`` each directory ``mkdir(parents=True)`` created for this
    delivery, innermost first, and only while still empty — so a directory
    that picked up an unrelated entry from anywhere else in the meantime is
    left alone rather than silently deleted. Best-effort: this runs after
    the delivery has already failed, and a directory that cannot be removed
    is not a reason to hide that failure. ``rmdir`` refuses a non-empty
    directory on its own, which is also why the walk stops at the first
    failure — a directory whose child is still there cannot be empty
    either."""
    for directory in directories:
        try:
            directory.rmdir()
        except OSError:
            break


def _refuse_a_file_that_left_the_repository(repository_dir: Path, destination: Path, candidate: str) -> None:
    """The containment check in ``paths.py`` runs before the lock is even
    taken, and both ``mkdir(parents=True)`` and ``os.link`` follow symlinks
    in the parent components. A symlink planted into the working tree
    between the check and the write would therefore have landed the
    artifact in another project's repository, with this librarian
    answering as though it were in this one.

    So the file that was just created is asked where it really is, and if
    the answer is outside this repository it is removed and the request is
    answered ``copy_failed``. The caller also removes any now-empty
    directories this delivery's own ``mkdir`` created along the way (V4
    audit Pass 2 row 29) — a nested ``requestedPath`` walking through the
    planted symlink would otherwise leave those behind in the victim
    repository even though the file itself is gone. No confirmation ever
    names a path in a repository the requester did not ask for.
    """
    try:
        real = destination.resolve()
    except OSError as err:  # pragma: no cover - resolve() on a created file
        raise DeliveryFailure(failures.COPY_FAILED,
                              f'copy to {candidate!r} could not be located after it was written: {err}') from err
    if repository_dir in real.parents:
        return
    try:
        destination.unlink()
    except OSError:
        pass
    raise DeliveryFailure(
        failures.COPY_FAILED,
        f'copy to {candidate!r} landed at {real}, outside the repository: a path component changed '
        f'between the containment check and the write, so the file was removed and nothing was delivered',
    )


def _collision_candidates(repository_dir: Path, requested: str):
    """REQ-04's adjustment rule, stated once, here.

    The requested path is used unchanged when nothing occupies it. When
    something unrelated does — a file with different content, or a
    directory of that name — ``-1``, ``-2``, ... is inserted **before the
    extension**, and the first free name wins:

        designs/mockup.png -> designs/mockup-1.png -> designs/mockup-2.png
        NOTES              -> NOTES-1              -> NOTES-2
        .gitignore         -> .gitignore-1         -> .gitignore-2

    The extension is the last dot in the file's name, and a leading dot is
    not one (``.gitignore`` is a name, not an extension). The rule is
    deterministic: the same repository state and the same requested path
    always produce the same answer.

    "Something unrelated" is the only case this can be: a file whose bytes
    ARE the artifact was already found and answered by REQ-03 step 4, which
    runs first.
    """
    if not paths.absolute_path(repository_dir, requested).exists():
        yield requested
    directory, _, name = requested.rpartition('/')
    stem, extension = _split_extension(name)
    for n in range(1, MAX_COLLISION_ATTEMPTS + 1):
        candidate = posixpath.join(directory, f'{stem}-{n}{extension}') if directory else f'{stem}-{n}{extension}'
        if not paths.absolute_path(repository_dir, candidate).exists():
            yield candidate


def _split_extension(name: str) -> tuple[str, str]:
    dot = name.rfind('.')
    if dot <= 0:
        return name, ''
    return name[:dot], name[dot:]


def _create_atomically(source: Path, destination: Path) -> None:
    """REQ-07's "MUST create the destination file atomically".

    The bytes are written to a temporary name in the **same directory**
    (same filesystem, so the publish below is a rename and not a copy),
    flushed to disk, and only then given their real name with ``os.link``,
    which fails rather than overwriting if the name is taken. No reader
    ever sees a partially written file at the destination, and no unrelated
    file is ever clobbered.

    ``os.link`` is the primitive because it is the one that both creates
    and refuses to overwrite. On a filesystem that cannot hard-link, the
    fallback claims the name with ``O_CREAT|O_EXCL`` — which has the same
    refuse-to-overwrite guarantee — and renames over its own empty
    placeholder.
    """
    temporary = destination.parent / f'.{destination.name}.librarian-{uuid.uuid4().hex}.tmp'
    try:
        with open(source, 'rb') as reader, open(temporary, 'wb') as writer:
            shutil.copyfileobj(reader, writer)
            writer.flush()
            os.fsync(writer.fileno())
        try:
            os.link(temporary, destination)
        except FileExistsError:
            raise
        except OSError:
            handle = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            os.close(handle)
            os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


# --- REQ-05: the record, written synchronously ---------------------------

def _record(request: DeliveryRequest, artifact, repository: str, path: str) -> ArtifactDelivery:
    """Create or correct the record for this (artifact, repository).

    ``delivered_at`` is not in ``defaults``, so it keeps the first
    delivery's time across a correction; ``updated_at`` moves. The unique
    constraint on the pair makes "create or correct" the only two outcomes
    there are.
    """
    now = timezone.now()
    record, _created = ArtifactDelivery.objects.update_or_create(
        artifact_id=artifact.id, repository=repository,
        defaults={
            'path': path,
            'requested_by': request.requested_by,
            'task_id': request.task_id,
            'updated_at': now,
        },
    )
    return record


# --- REQ-06's first reason, and the artifact's bytes ---------------------

def _artifact_or_failure(artifact_id: str) -> Artifact:
    try:
        parsed = uuid.UUID(artifact_id)
    except (AttributeError, TypeError, ValueError):
        raise DeliveryFailure(failures.UNKNOWN_ARTIFACT,
                              f'{artifact_id!r} is not a canonical artifact id') from None
    artifact = Artifact.objects.filter(id=parsed).first()
    if artifact is None:
        raise DeliveryFailure(failures.UNKNOWN_ARTIFACT, f'no artifact is registered under id {parsed}')
    return artifact


def _source_file(artifact: Artifact) -> Path:
    """The artifact's file on the mounted volume. Its absence is a copy
    that failed rather than an unknown artifact: the id resolved, and it is
    the bytes that are missing."""
    source = Path(artifact.file.path)
    if not source.is_file():
        raise DeliveryFailure(failures.COPY_FAILED,
                              f'artifact {artifact.id} has no file on the artifact volume at {artifact.path}')
    return source


def _confirmation(path: str, action: str, record: ArtifactDelivery) -> dict[str, Any]:
    """``deliveredAt`` is when this artifact first reached this
    repository, not when this request was answered: a repeat request
    reports the original delivery, which is the fact the requester
    asked about."""
    return {'path': path, 'action': action, 'deliveredAt': record.delivered_at.isoformat()}
