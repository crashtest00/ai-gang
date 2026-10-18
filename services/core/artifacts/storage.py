"""
Volume-backed storage for artifact files — artifact-ingress.md REQ-02
(the stored file is the record), REQ-04 (re-upload replaces content under
the same id and path) and REQ-05 (values that become paths are validated).

Layout (the open question the spec leaves to implementation, §6.1):

    <ARTIFACT_ROOT>/<first two characters of the id>/<the id>

e.g. artifact ``3f2c1e5a-7b41-4f0a-9d2e-6c8b0a1d4e77`` is stored at
``<ARTIFACT_ROOT>/3f/3f2c1e5a-7b41-4f0a-9d2e-6c8b0a1d4e77``. The path is
derived from the canonical id and from nothing else — see ``upload_to``.
A human on the host takes the first two characters of an id, and the file
is there under its full id; that predictability is what REQ-02's in-place
editing depends on. The two-character shard exists so the root does not
become one directory holding every artifact in the instance.

Files carry **no extension**: the original filename never reaches the
filesystem at all (REQ-05), so there is nothing to take an extension
from, and nothing on the volume that a tool could dispatch on by name.
This is the same "custody without interpretation" line the PRD draws
(§8) expressed in the layout.
"""

from __future__ import annotations

import os
import uuid

from django.conf import settings
from django.core.exceptions import SuspiciousFileOperation
from django.core.files.storage import FileSystemStorage

# Characters of the canonical id used as the shard directory name.
SHARD_LENGTH = 2


def _validated_artifact_id(value) -> str:
    """REQ-05's validation for the one value that DOES become a path
    component: the canonical id.

    The returned string is re-derived from a parsed ``uuid.UUID`` rather
    than echoed back from the input, so what reaches the filesystem is
    always 32 hex digits and four hyphens whatever was handed in — a path
    separator, a ``..``, a NUL or a leading ``/`` cannot survive
    ``uuid.UUID()``. Anything that will not parse is refused outright,
    with Django's own ``SuspiciousFileOperation`` (which Django's request
    machinery already renders as a 400 rather than a 500).
    """
    try:
        parsed = uuid.UUID(str(value))
    except (AttributeError, TypeError, ValueError) as err:
        raise SuspiciousFileOperation(
            f'artifact id {value!r} is not a canonical id and cannot be used as a path component'
        ) from err
    return str(parsed)


def artifact_relative_path(artifact_id) -> str:
    """The storage-relative path for an artifact id — see the module
    docstring for the layout, and ``_validated_artifact_id`` for what
    makes it safe."""
    identifier = _validated_artifact_id(artifact_id)
    return f'{identifier[:SHARD_LENGTH]}/{identifier}'


def upload_to(instance, filename: str) -> str:
    """``FileField(upload_to=...)`` — the enforcement point for REQ-05.

    ``filename`` is the name the uploading client supplied. It is accepted
    and **ignored**: the path is a pure function of the canonical id, so
    an upload named ``../../etc/passwd`` has no route to the filesystem at
    all. The name itself is kept verbatim on the record
    (``Artifact.original_filename``) as an opaque string.
    """
    return artifact_relative_path(instance.id)


class ArtifactFileStorage(FileSystemStorage):
    """``FileSystemStorage`` rooted at ``settings.ARTIFACT_ROOT``, with
    Django's collision-renaming turned off.

    Two deliberate departures from the base class:

    - **``location``/``base_location`` are plain properties, not
      ``cached_property``.** ``StorageSettingsMixin._clear_cached_properties``
      only invalidates the base class's caches for ``MEDIA_ROOT`` and
      friends, so a cached location would ignore ``ARTIFACT_ROOT`` and
      silently pin the first value it ever saw.

    - **Re-upload overwrites in place (REQ-04).** Django's default is to
      keep both files by picking a new, random name on collision, which
      would change an artifact's path on every re-upload and break "the
      id, the path, and every reference already made to it" — so
      ``get_available_name`` returns the name unchanged, and
      ``OS_OPEN_FLAGS`` trades ``O_EXCL`` (fail if it exists) for
      ``O_TRUNC`` (truncate and rewrite).
    """

    # O_EXCL replaced by O_TRUNC: same path, new bytes (REQ-04).
    OS_OPEN_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_BINARY', 0)

    @property
    def base_location(self):
        return self._value_or_setting(self._location, settings.ARTIFACT_ROOT)

    @property
    def location(self):
        return os.path.abspath(self.base_location)

    def get_available_name(self, name, max_length=None):
        return name

    def _save(self, name, content):
        """An upload larger than ``FILE_UPLOAD_MAX_MEMORY_SIZE`` is spooled
        to a temp file, and ``FileSystemStorage._save`` then moves it with
        ``file_move_safe(..., allow_overwrite=False)``, which raises
        ``FileExistsError`` — whereupon ``_save``'s retry loop asks
        ``get_available_name`` for a different name, gets the same one
        back, and spins forever. Unlinking first is what keeps the large
        upload path on the same overwrite-in-place contract the small one
        gets from ``O_TRUNC``.
        """
        if hasattr(content, 'temporary_file_path'):
            full_path = self.path(name)
            if os.path.exists(full_path):
                os.remove(full_path)
        return super()._save(name, content)


def artifact_storage() -> ArtifactFileStorage:
    """``FileField(storage=...)`` takes this callable rather than an
    instance, so the migration records a stable dotted path to it instead
    of a serialized storage object."""
    return ArtifactFileStorage()
