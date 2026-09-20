"""
The delivery record — librarian.md REQ-05.

"The librarian MUST keep a durable record per artifact and repository
holding the delivered path, the requester, the time, and ``task_id`` where
supplied, written synchronously as part of handling the request."

It is an index, not the truth. The filesystem says where the file is; this
table says where the librarian last put it, so the common case answers
without walking the repository. Where the two disagree the filesystem wins
and this row is corrected (REQ-03 step 4, ``delivery.py``).

The table is this app's own: ``artifact_delivery``, no ``work_item_``
prefix. The Django instance holds more than work items now.
"""

from __future__ import annotations

from django.db import models
from django.utils import timezone


class ArtifactDelivery(models.Model):
    """One artifact, in one repository, at one path.

    REQ-02 ("an artifact MUST occupy at most one path within a given
    repository") is enforced at the schema level by
    ``uniq_artifact_delivery_pair`` below — a real UNIQUE constraint in
    ``migrations/0001_initial.py``, not a convention the application
    promises to keep. Two rows for the same pair cannot exist, so there is
    no shape in which this table records two paths for one artifact in one
    repository.
    """

    id = models.BigAutoField(primary_key=True)

    # A foreign key, not a loose UUID: the artifact record is in the same
    # Postgres instance (PRD §3), so "does this id resolve" is answerable
    # in the database as well as in the REQ-06 check that runs before any
    # of this. CASCADE because a delivery record is meaningless without the
    # artifact it indexes; the delivered FILE is unaffected either way,
    # which is the record-versus-filesystem asymmetry REQ-05 is about.
    #
    # ``related_name='+'`` creates no reverse accessor on ``Artifact``:
    # this app never needs one, and the artifact record's own surface is
    # artifact ingress's to define, not something a second app adds to
    # from the side (``tests/test_artifacts_upload.py`` asserts that
    # surface field by field). The database-level foreign key is
    # unaffected.
    artifact = models.ForeignKey(
        'artifacts.Artifact', on_delete=models.CASCADE, db_column='artifact_id', related_name='+',
    )

    # The destination repository, as named in the request: one directory
    # name directly under settings.PROJECTS_ROOT (see paths.py).
    repository = models.TextField()

    # Repository-relative, forward slashes, never absolute — the path the
    # copy or the lookup actually produced (REQ-04), never the requested
    # one.
    path = models.TextField()

    # REQ-05's "the requester, the time, and task_id where supplied".
    requested_by = models.TextField()
    task_id = models.TextField(null=True, blank=True)
    delivered_at = models.DateTimeField(default=timezone.now)
    # Moves when REQ-03 step 4 corrects `path`, or when a later request
    # re-delivers a file that went missing. `delivered_at` keeps the first
    # delivery's time.
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'artifact_delivery'
        constraints = [
            models.UniqueConstraint(fields=['artifact', 'repository'], name='uniq_artifact_delivery_pair'),
        ]
        indexes = [
            models.Index(fields=['repository'], name='idx_artifact_delivery_repo'),
        ]

    def __str__(self) -> str:
        return f'{self.artifact_id} -> {self.repository}:{self.path}'
