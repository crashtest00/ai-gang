"""REQ-06 (declared formats; visible failures) and REQ-01 (byte identity),
through the real path: Core's HTTP read surface, the job store, the spool
and the extractor process."""

import hashlib

import pytest

from extraction.models import OUTCOME_EXTRACTED, OUTCOME_FAILED, ExtractionJob, ExtractionRecord
from extraction import jobs
from extractor import formats
from tests.extraction_support import (  # noqa: F401 - fixtures
    PNG, core, drain, extractor, heavy_html, make_docx, make_pdf, spool_dir, start_extractor, stop,
)

pytestmark = pytest.mark.django_db(transaction=True)

# One file per declared format; the format is decided from the content, so
# only Markdown (whose content is plain text) is told apart by its filename,
# and the others are uploaded with no filename at all.
SAMPLES = [
    (formats.PDF, make_pdf('Quarterly heron census'), '', 'Quarterly heron census'),
    (formats.DOCX, make_docx('Minutes of the heron board', 'Second paragraph'), '', 'Minutes of the heron board'),
    (formats.TEXT, b'Plain notes about herons.\n', '', 'Plain notes about herons.'),
    (formats.MARKDOWN, b'# Herons\n\nA *wading* bird.\n', 'herons.md', '# Herons'),
    (formats.HTML, b'<!DOCTYPE html><html><head><title>Herons</title><script>x()</script></head>'
                   b'<body><p>Grey heron &amp; egret</p></body></html>', '', 'Grey heron & egret'),
    (formats.CSV, b'species,count\nheron,4\negret,"1,200"\n', '', 'egret\t1,200'),
    (formats.TSV, b'species\tcount\nheron\t4\negret\t12\n', '', 'heron\t4'),
]


@pytest.mark.parametrize('fmt,content,filename,phrase', SAMPLES, ids=[s[0] for s in SAMPLES])
def test_each_declared_format_is_extracted_with_its_format_recorded(core, spool_dir, extractor, fmt, content,
                                                                     filename, phrase):
    artifact_id = core.upload(content, filename)
    jobs.enqueue(artifact_id, 'created')
    assert drain(spool_dir) == ['done']
    record = ExtractionRecord.objects.get(artifact_id=artifact_id)
    assert record.outcome == OUTCOME_EXTRACTED
    assert record.format == fmt
    assert phrase in record.text
    assert record.failure_reason is None


def test_the_declared_formats_are_exactly_the_minimum_set():
    assert set(formats.DECLARED_FORMATS) == {
        formats.PDF, formats.DOCX, formats.TEXT, formats.MARKDOWN, formats.HTML, formats.CSV, formats.TSV}


@pytest.mark.parametrize('content,filename,expected', [
    (make_docx('x'), 'renamed.bin', formats.DOCX),       # content beats a misleading filename
    (make_pdf('x'), 'report.txt', formats.PDF),
    (b'a\tb\tc\n1\t2\t3\n4\t5\t6\n', '', formats.TSV),  # sniffed with no filename
    (b'<html><body>x</body></html>', '', formats.HTML),
    (b'Notes, with commas, in prose.\nAnd a second line.\n', '', formats.TEXT),
    (b'a,b\n1,2\n', 'data.csv', formats.CSV),
    ('caf\xe9 au lait'.encode('cp1252'), '', formats.TEXT),  # legacy single-byte text
    (b'mentions %PDF- later\n%PDF-1.4', '', formats.TEXT),
])
def test_the_format_is_decided_from_the_content_and_the_filename(content, filename, expected):
    assert formats.decide_format(content, filename) == (expected, None)


def _failed(core, spool_dir, content, filename=''):
    artifact_id = core.upload(content, filename)
    before = hashlib.sha256(core.content(artifact_id)).hexdigest()
    jobs.enqueue(artifact_id, 'created')
    assert drain(spool_dir) == ['done']
    record = ExtractionRecord.objects.get(artifact_id=artifact_id)
    assert hashlib.sha256(core.content(artifact_id)).hexdigest() == before == record.source_sha256
    return record


def test_an_image_is_recorded_as_a_failure_with_its_reason(core, spool_dir, extractor):
    record = _failed(core, spool_dir, PNG, 'photo.png')
    assert record.outcome == OUTCOME_FAILED
    assert record.format == 'image/png'
    assert 'unsupported format' in record.failure_reason and 'image' in record.failure_reason
    assert record.text is None


@pytest.mark.parametrize('content,reason', [
    (b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1' + b'\x00' * 64, 'legacy Office binary'),
    (bytes(range(256)) * 8, 'unrecognised binary'),
])
def test_other_undeclared_formats_are_recorded_as_failures(core, spool_dir, extractor, content, reason):
    record = _failed(core, spool_dir, content)
    assert record.outcome == OUTCOME_FAILED and reason in record.failure_reason


def test_a_file_the_parser_rejects_is_a_failure_with_the_parser_error(core, spool_dir, extractor):
    record = _failed(core, spool_dir, b'%PDF-1.4\n this is not a pdf at all')
    assert record.outcome == OUTCOME_FAILED
    assert record.format == formats.PDF
    assert record.failure_reason.startswith('extraction failed:')


def test_an_empty_file_is_a_success_with_empty_text_distinct_from_a_failure(core, spool_dir, extractor):
    empty = _failed(core, spool_dir, b'')
    image = _failed(core, spool_dir, PNG)
    assert (empty.outcome, empty.text, empty.failure_reason) == (OUTCOME_EXTRACTED, '', None)
    assert (image.outcome, image.text) == (OUTCOME_FAILED, None)


def test_a_file_that_crashes_the_extractor_is_a_failure_with_its_reason(core, spool_dir):
    # The per-file child is held to one CPU-second, so this file's parse is
    # killed by the kernel (SIGXCPU) mid-file: a real crash of the process
    # parsing it.
    process = start_extractor(spool_dir, '--cpu-limit-seconds', '1')
    try:
        record = _failed(core, spool_dir, heavy_html(), 'big.html')
    finally:
        stop(process)
    assert record.outcome == OUTCOME_FAILED
    assert record.failure_reason.startswith('extractor crashed (killed by SIGXCPU)')
    assert record.text is None


def test_a_file_over_the_time_limit_is_a_failure_naming_the_limit(core, spool_dir, extractor, settings):
    settings.MEMORY_EXTRACTION_TIME_LIMIT_SECONDS = 1
    record = _failed(core, spool_dir, heavy_html(), 'big.html')
    assert record.outcome == OUTCOME_FAILED
    assert record.failure_reason == 'time limit exceeded: extraction ran longer than 1 seconds'


def test_a_missing_stored_file_is_a_failure_not_a_drop(core, spool_dir, extractor):
    artifact_id = core.upload(b'gone soon', 'a.txt')
    core.artifacts[artifact_id]['missing'] = True
    jobs.enqueue(artifact_id, 'created')
    drain(spool_dir)
    record = ExtractionRecord.objects.get(artifact_id=artifact_id)
    assert record.outcome == OUTCOME_FAILED and 'missing' in record.failure_reason
    assert record.source_sha256 is None


def test_req01_the_stored_file_is_byte_identical_after_extraction(core, spool_dir, extractor):
    supplied = make_docx('Byte identity check')
    artifact_id = core.upload(supplied, 'check.docx')
    jobs.enqueue(artifact_id, 'created')
    drain(spool_dir)
    stored = core.content(artifact_id)
    assert stored == supplied
    record = ExtractionRecord.objects.get(artifact_id=artifact_id)
    assert hashlib.sha256(stored).hexdigest() == hashlib.sha256(supplied).hexdigest() == record.source_sha256
    assert record.text == 'Byte identity check'
    assert ExtractionJob.objects.get(artifact_id=artifact_id).state == 'done'
