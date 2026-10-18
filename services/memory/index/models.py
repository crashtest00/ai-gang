"""The corpus index (Corpus Indexing REQ-02, REQ-05, REQ-08, REQ-09), in
Memory's PostgreSQL.

**A unit** (``IndexUnit``) is one indexed passage: its text, the class of
the source it came from, its scope, and a reference that resolves to the
source record and the location in it (REQ-02). The database refuses a unit
that cannot carry one: ``index_unit_reference_*`` CHECK constraints name
the keys each class's reference must hold (for Core's four classes, a
non-empty ``fields`` naming the fields of Core's read its text came from,
``index.render``), ``index_unit_scope_matches_class``
ties the scope to the class (REQ-08: `platform` only on the platform
corpus, `project` on every one of the seven classes), and
``index_unit_text_not_empty`` refuses an empty passage. ``search`` is a
generated ``tsvector`` over the text (``SEARCH``), with a GIN index, and
``embedding`` is the text's vectors from the local embedding model
(``index.embedding``); ``index_unit_embedding_whole_vectors`` refuses a unit
without one. Retrieval fuses the two rankings (see ``index/__init__.py``).

**Everything else is bookkeeping about the index itself**, never a copy of
a source record (REQ-03): what each source was last indexed from
(``IndexSource``), each class's read cursor and whether an event has asked
for a catch-up that has not run yet (``IndexCursor``, REQ-04, REQ-05),
library files announced but not yet extracted (``PendingArtifact``,
REQ-05), which library files a work item's specification link names
(``SpecificationLink``, REQ-01's class 1), recorded failures
(``IndexFailure``, REQ-09) and the indexer's heartbeat and platform commit
(``IndexState``). Dropping every table here and rebuilding from the sources
restores the index (REQ-03).
"""

from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVector, SearchVectorField
from django.db import models
from django.db.models import Case, F, Func, Q, Value, When
from django.db.models.functions import Length, Mod
from django.db.models.lookups import Exact, GreaterThan

from .embedding import MODEL

# One vector's packed size: the model's width in float32.
EMBEDDING_BYTES = MODEL.dim * 4

# --- Source classes (REQ-01) and the platform corpus (REQ-08) ---------------

CLASS_SPECIFICATION = 'specification'
CLASS_WORK_ITEM = 'work_item'
CLASS_COMMENT = 'work_item_comment'
CLASS_DELIVERY = 'delivery_artifact'
CLASS_A2A = 'a2a_message'
CLASS_SESSION = 'session'
CLASS_LIBRARY = 'library_file'
PROJECT_CLASSES = (CLASS_SPECIFICATION, CLASS_WORK_ITEM, CLASS_COMMENT, CLASS_DELIVERY, CLASS_A2A,
                   CLASS_SESSION, CLASS_LIBRARY)
CLASS_PLATFORM = 'platform_document'
CLASSES = PROJECT_CLASSES + (CLASS_PLATFORM,)

SCOPE_PROJECT = 'project'
SCOPE_PLATFORM = 'platform'
SCOPES = (SCOPE_PROJECT, SCOPE_PLATFORM)


def _keys(*keys):
    return Q(reference__has_keys=list(keys))


_LOCATED = ('offset', 'end')
_LINES = ('lineStart', 'lineEnd')
# A Core record's unit names the fields of Core's read its text came from
# (index.render, Knowledge Retrieval REQ-04): ``fields`` is a list whose first
# entry has a ``path``. ``IS NOT NULL`` rather than ``?``, so an empty list or
# an object reads false, never NULL (a NULL CHECK passes).
# ``retrieval.sources.resolvable`` checks every entry.
_NAMES_FIELDS = _keys('fields') & Q(reference__fields__0__path__isnull=False)


# What a unit is searched by: its text and, for a delivery artifact, its text
# again with every run of non-alphanumerics read as a space. PostgreSQL's
# english parser keeps a URL or a path as one lexeme, so without the second
# reading no plain question could match the words inside a delivery artifact's
# URL. The second reading is search-only: it is not in ``text``, so a returned
# passage holds only what its record holds (REQ-02).
SEARCH_WORDS = Case(
    When(source_class=CLASS_DELIVERY,
         then=Func(F('text'), Value('[^[:alnum:]]+'), Value(' '), Value('g'), function='regexp_replace',
                   output_field=models.TextField())),
    default=Value(''), output_field=models.TextField())
SEARCH = SearchVector('text', SEARCH_WORDS, config='english')


class IndexUnit(models.Model):
    source_class = models.CharField(max_length=32)
    scope = models.CharField(max_length=16)
    # The record a unit came from, as `<kind>:<id>[:<part>]` (see
    # index/store.py `source_key`); the units of one source are replaced
    # and removed together.
    source_key = models.CharField(max_length=1200)
    # Set on a work item's units and on its comments' and delivery
    # artifacts', so deleting the work item removes them all (REQ-07).
    work_item_id = models.UUIDField(null=True, blank=True)
    reference = models.JSONField()
    text = models.TextField()
    position = models.PositiveIntegerField(default=0)
    # False once a library file's re-upload is announced and before its new
    # text is indexed: the units are no longer current (REQ-04, PRD §7.3).
    current = models.BooleanField(default=True)
    # When the source record was written, as the source states it.
    written_at = models.DateTimeField(null=True, blank=True)
    indexed_at = models.DateTimeField()
    search = models.GeneratedField(expression=SEARCH, output_field=SearchVectorField(), db_persist=True)
    # The unit's text embedded by the local model (index.embedding): one
    # L2-normalised float32 vector per window of the text, packed. Derived
    # like ``search``, written with the unit (index.store.replace_units), and
    # rebuilt with it (REQ-03).
    embedding = models.BinaryField()

    class Meta:
        db_table = 'index_unit'
        indexes = [
            models.Index(fields=['source_key'], name='idx_index_unit_source'),
            models.Index(fields=['work_item_id'], name='idx_index_unit_work_item'),
            models.Index(fields=['source_class', 'scope'], name='idx_index_unit_class_scope'),
            GinIndex(fields=['search'], name='idx_index_unit_search'),
        ]
        constraints = [
            models.CheckConstraint(check=Q(source_class__in=CLASSES), name='index_unit_class_valid'),
            models.CheckConstraint(check=Q(scope__in=SCOPES), name='index_unit_scope_valid'),
            # REQ-08: the stamp is set by where the unit came from.
            models.CheckConstraint(
                check=(Q(scope=SCOPE_PROJECT, source_class__in=PROJECT_CLASSES)
                       | Q(scope=SCOPE_PLATFORM, source_class=CLASS_PLATFORM)),
                name='index_unit_scope_matches_class'),
            models.CheckConstraint(check=~Q(text=''), name='index_unit_text_not_empty'),
            # Every unit has an embedding of whole vectors of the model's width.
            models.CheckConstraint(
                check=Q(GreaterThan(Length('embedding'), 0)) & Q(Exact(Mod(Length('embedding'), EMBEDDING_BYTES), 0)),
                name='index_unit_embedding_whole_vectors'),
            # REQ-02: each class's reference carries what resolves it.
            models.CheckConstraint(
                check=(~Q(source_class__in=(CLASS_LIBRARY, CLASS_SPECIFICATION))
                       | (_keys('artifactId', 'stamp', *_LINES, *_LOCATED)
                          & Q(reference__stamp__has_keys=['updatedAt', 'sha256']))),
                name='index_unit_reference_library'),
            models.CheckConstraint(
                check=(~Q(source_class=CLASS_SESSION)
                       | (_keys('sessionId', 'file', 'turn', 'lines', *_LINES, *_LOCATED)
                          & (~Q(reference__has_key='parentSessionId') | Q(reference__has_key='toolUseId')))
                       | _keys('project', 'path', 'archivePath', 'digest', *_LINES, *_LOCATED)),
                name='index_unit_reference_session'),
            models.CheckConstraint(
                check=~Q(source_class=CLASS_WORK_ITEM) | (_keys('workItemId', 'section', *_LOCATED) & _NAMES_FIELDS),
                name='index_unit_reference_work_item'),
            models.CheckConstraint(
                check=~Q(source_class=CLASS_COMMENT) | (_keys('commentId', 'workItemId', *_LOCATED) & _NAMES_FIELDS),
                name='index_unit_reference_comment'),
            models.CheckConstraint(
                check=(~Q(source_class=CLASS_DELIVERY)
                       | (_keys('deliveryArtifactId', 'workItemId', *_LOCATED) & _NAMES_FIELDS)),
                name='index_unit_reference_delivery'),
            models.CheckConstraint(
                check=~Q(source_class=CLASS_A2A) | (_keys('taskId', 'messageId', *_LOCATED) & _NAMES_FIELDS),
                name='index_unit_reference_a2a'),
            models.CheckConstraint(
                check=~Q(source_class=CLASS_PLATFORM) | _keys('commit', 'path', *_LINES, *_LOCATED),
                name='index_unit_reference_platform'),
        ]


class IndexSource(models.Model):
    """What a source was last indexed from: a digest of its rendering, a
    note's or transcript's digest, a library file's extraction stamp. An
    unchanged source is not re-indexed."""

    source_key = models.CharField(max_length=1200, primary_key=True)
    source_class = models.CharField(max_length=32)
    work_item_id = models.UUIDField(null=True, blank=True, db_index=True)
    stamp = models.JSONField()
    indexed_at = models.DateTimeField()

    class Meta:
        db_table = 'index_source'


class IndexCursor(models.Model):
    """One followed read (REQ-04): a Core enumeration, the archive's session
    listing or notes read, or the extraction-record follow. An event that
    asks for a catch-up raises ``requested``; a catch-up that finishes sets
    ``completed`` to the value it started from, so the read is behind while
    ``requested > completed`` (REQ-05). ``caught_up`` is false until the
    read has run once to its end: until then the class is "not indexed
    yet" (REQ-01)."""

    name = models.CharField(max_length=64, primary_key=True)
    cursor = models.TextField(blank=True, default='')
    requested = models.BigIntegerField(default=0)
    completed = models.BigIntegerField(default=0)
    caught_up = models.BooleanField(default=False)
    last_round_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'index_cursor'


class PendingArtifact(models.Model):
    """A library file whose upload was announced and whose extraction for
    that upload is not yet recorded: "not indexed yet" (REQ-05)."""

    artifact_id = models.UUIDField(primary_key=True)
    updated_at = models.DateTimeField()
    action = models.CharField(max_length=32)
    received_at = models.DateTimeField()

    class Meta:
        db_table = 'index_pending_artifact'


class SpecificationLink(models.Model):
    """A work item's specification link as Core's enumeration last returned
    it: the library files named here are class 1, specifications."""

    work_item_id = models.UUIDField(primary_key=True)
    artifact_id = models.UUIDField(db_index=True)

    class Meta:
        db_table = 'index_specification_link'


class IndexFailure(models.Model):
    """REQ-09: a record that failed to index, why, and what a retry needs."""

    source_key = models.CharField(max_length=1200, unique=True)
    source_class = models.CharField(max_length=32)
    reason = models.TextField()
    payload = models.JSONField(default=dict)
    attempts = models.PositiveIntegerField(default=1)
    first_failed_at = models.DateTimeField()
    last_failed_at = models.DateTimeField()

    class Meta:
        db_table = 'index_failure'
        constraints = [models.CheckConstraint(check=~Q(reason=''), name='index_failure_reason_not_empty')]


class IndexState(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    # The Source commit the platform corpus was last indexed from (I-23).
    platform_commit = models.CharField(max_length=64, blank=True, default='')

    class Meta:
        db_table = 'index_state'
        constraints = [models.CheckConstraint(check=Q(id=1), name='index_state_single_row')]
