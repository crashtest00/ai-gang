"""REQ-02 (the source stamp and its read protocol) and REQ-03 (a replaced
file's old text is never current), with brief I-05."""

import hashlib

import pytest

from extraction import core_client, jobs
from extraction.models import ExtractionJob, ExtractionRecord
from tests.extraction_support import core, drain, extractor, spool_dir  # noqa: F401 - fixtures

pytestmark = pytest.mark.django_db(transaction=True)


def test_req02_the_record_carries_the_id_the_read_time_updated_at_and_the_hash_of_the_stored_bytes(
        core, spool_dir, extractor):
    artifact_id = core.upload(b'Stamped content.\n', 'stamp.txt')
    jobs.enqueue(artifact_id, 'created')
    drain(spool_dir)
    record = ExtractionRecord.objects.get(artifact_id=artifact_id)
    assert str(record.artifact_id) == artifact_id
    assert record.source_updated_at == core.updated_at(artifact_id)
    assert record.source_sha256 == hashlib.sha256(core.content(artifact_id)).hexdigest()
    # The protocol's reads: record, bytes, record (then one more record read
    # at completion, REQ-07).
    assert core.byte_reads[artifact_id] == 1
    assert core.record_reads[artifact_id] == 3


def test_req02_a_reupload_between_the_record_read_and_the_byte_read_records_no_stamp_for_that_attempt(
        core, spool_dir, extractor):
    artifact_id = core.upload(b'First version.\n', 'v.txt')
    old_updated_at = core.updated_at(artifact_id)
    seen = []

    def reupload_after_first_record_read(read_id, nth):
        if read_id == artifact_id and nth == 1:
            core.reupload(artifact_id, b'Second version.\n')

    core.after_record_read = reupload_after_first_record_read
    # Spy on every stamp the job store is given, at the point it is saved.
    original_save = ExtractionJob.save

    def spy(self, *args, **kwargs):
        if self.stamp_updated_at is not None:
            seen.append((self.stamp_updated_at, self.stamp_sha256))
        return original_save(self, *args, **kwargs)

    ExtractionJob.save = spy
    try:
        jobs.enqueue(artifact_id, 'created')
        assert drain(spool_dir) == ['done']
    finally:
        ExtractionJob.save = original_save

    new_updated_at = core.updated_at(artifact_id)
    new_sha = hashlib.sha256(b'Second version.\n').hexdigest()
    # The first attempt read old updatedAt, new bytes, new updatedAt: no stamp.
    assert core.byte_reads[artifact_id] == 2
    assert all(stamp_at != old_updated_at for stamp_at, _ in seen)
    assert set(seen) == {(new_updated_at, new_sha)}
    record = ExtractionRecord.objects.get(artifact_id=artifact_id)
    assert (record.source_updated_at, record.source_sha256) == (new_updated_at, new_sha)
    assert record.text == 'Second version.\n'


def test_req02_reextraction_is_possible_at_any_time_from_the_library_alone(core, spool_dir, extractor):
    artifact_id = core.upload(b'Re-extract me.\n', 'r.txt')
    jobs.enqueue(artifact_id, 'created')
    drain(spool_dir)
    ExtractionRecord.objects.all().delete()  # the text is derived: losing it costs time only
    jobs.enqueue(artifact_id, 'sweep:missing')
    drain(spool_dir)
    assert ExtractionRecord.objects.get(artifact_id=artifact_id).text == 'Re-extract me.\n'


def test_req03_a_reupload_makes_the_old_text_not_current_at_once_and_the_new_text_replaces_it(
        core, spool_dir, extractor):
    artifact_id = core.upload(b'The password is periwinkle.\n', 'secret.txt')
    jobs.enqueue(artifact_id, 'created')
    drain(spool_dir)
    record = ExtractionRecord.objects.get(artifact_id=artifact_id)
    assert record.is_current_for(core_client.get_record(artifact_id).updated_at)

    core.reupload(artifact_id, b'The password was rotated.\n')
    # Immediately, before re-extraction: the comparison a consumer makes from
    # this record and Core's present record says not current.
    present = core_client.get_record(artifact_id)
    record.refresh_from_db()
    assert not record.is_current_for(present.updated_at)
    assert 'periwinkle' in record.text  # still there, but not current

    jobs.enqueue(artifact_id, 'replaced')
    drain(spool_dir)
    record.refresh_from_db()
    assert record.is_current_for(core_client.get_record(artifact_id).updated_at)
    assert record.text == 'The password was rotated.\n'
    assert 'periwinkle' not in record.text
    assert ExtractionRecord.objects.filter(artifact_id=artifact_id).count() == 1


def test_i05_a_stamp_pairing_the_old_updated_at_with_new_bytes_is_replaced_by_the_replaced_event_job(
        core, spool_dir, extractor):
    artifact_id = core.upload(b'Old bytes.\n', 'i05.txt')
    jobs.enqueue(artifact_id, 'created')
    drain(spool_dir)
    old_updated_at = core.updated_at(artifact_id)

    # A re-upload writes its bytes before its row commits; a sweep or job
    # reading in between pairs the old updatedAt with the new hash.
    core.write_bytes_only(artifact_id, b'New bytes.\n')
    jobs.enqueue(artifact_id, 'sweep:changed')
    drain(spool_dir)
    record = ExtractionRecord.objects.get(artifact_id=artifact_id)
    assert record.source_updated_at == old_updated_at
    assert record.source_sha256 == hashlib.sha256(b'New bytes.\n').hexdigest()

    new_updated_at = core.commit_row(artifact_id)
    assert not record.is_current_for(new_updated_at)  # updatedAt is the only currency key
    jobs.enqueue(artifact_id, 'replaced')
    drain(spool_dir)
    record.refresh_from_db()
    assert record.source_updated_at == new_updated_at
    assert record.text == 'New bytes.\n'
