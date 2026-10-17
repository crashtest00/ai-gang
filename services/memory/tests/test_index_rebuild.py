"""Corpus Indexing REQ-03: the index is derived and fully rebuildable.
Dropped and rebuilt from the authoritative records alone, it holds the
same units for every class (a library file's from the retained extraction
record, not by parsing the stored file again), and no source record
changes during indexing or rebuild. The comparison of retrieval results for
a fixed question set (PRD AC-10) runs through track 4's route; here the
units themselves are compared, which is what retrieval reads."""

import json
from pathlib import Path

import pytest
from django.core.management import call_command

from archive.models import Note, Session, SessionFile
from extraction.models import ExtractionJob, ExtractionRecord
from index.models import IndexUnit
from tests.index_support import (  # noqa: F401 - fixtures
    archive_api, checkout, core, index_once, redis_client, seeded,
)

pytestmark = pytest.mark.django_db(transaction=True)


def _units():
    return sorted((u.source_class, u.scope, u.source_key, json.dumps(u.reference, sort_keys=True), u.text,
                   u.current) for u in IndexUnit.objects.all())


def _sources(core, archive_root):
    files = {str(p.relative_to(archive_root)): p.read_bytes() for p in Path(archive_root).rglob('*') if p.is_file()}
    return {
        'core': core.snapshot(),
        'extraction': sorted(ExtractionRecord.objects.values_list('artifact_id', 'source_updated_at',
                                                                  'source_sha256', 'outcome', 'text')),
        'sessions': sorted(Session.objects.values_list('session_id', 'status', 'change_seq')),
        'files': sorted(SessionFile.objects.values_list('path', 'digest', 'bytes_read')),
        'notes': sorted(Note.objects.values_list('path', 'digest', 'removed')),
        'archive': files,
    }


def test_req03_drop_and_rebuild_restores_every_unit_and_changes_no_source(seeded, core, roots):
    archive_root = roots[1]
    before = _sources(core, archive_root)
    jobs = ExtractionJob.objects.count()
    index_once()
    indexed = _units()
    assert {row[0] for row in indexed} == {'specification', 'library_file', 'work_item', 'work_item_comment',
                                          'delivery_artifact', 'a2a_message', 'session', 'platform_document'}
    assert _sources(core, archive_root) == before

    call_command('rebuild_index')
    assert _units() == indexed
    assert _sources(core, archive_root) == before
    assert ExtractionJob.objects.count() == jobs        # no file was sent to extraction again


def test_req03_the_index_tables_dropped_entirely_are_rebuilt_from_source(seeded, core, roots):
    index_once()
    indexed = _units()
    call_command('migrate', 'index', 'zero', verbosity=0)
    call_command('migrate', 'index', verbosity=0)
    assert IndexUnit.objects.count() == 0
    call_command('rebuild_index')
    assert _units() == indexed
