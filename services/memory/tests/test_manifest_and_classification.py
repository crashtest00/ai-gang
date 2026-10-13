"""REQ-04 (the manifest's fields) and REQ-05 (tolerant classification, the
per-version drift counts, the harness module callable by other code)."""

import json
from pathlib import Path

import pytest
from django.test import Client

from archive import harness
from archive.copier import digest_of
from archive.models import Session
from tests.support import Session as Src, completed_run, conv, cost_state, dequeue, enqueue, line

pytestmark = pytest.mark.django_db

FIELDS = {'session_id', 'project', 'source_path', 'files', 'first_seen', 'last_growth', 'harness_versions',
          'status', 'record_counts', 'quiet_by', 'pull_requests', 'commits'}


def counts_read():
    return Client().get('/counts').json()['versions']


def test_a_finished_sessions_manifest_carries_every_field_and_true_digests(roots, copier):
    harness_root, archive = roots
    s = Src(harness_root)
    s.append(completed_run())
    s.write('subagents/agent-a.meta.json', b'{}')
    copier.reconcile()
    manifest = Client().get(f'/sessions/{s.id}').json()
    assert FIELDS <= set(manifest)
    assert manifest['status'] == 'quiet' and manifest['quiet_by'] == 'end_record'
    for entry in manifest['files']:
        assert entry['digest'] == digest_of(Path(archive) / entry['archive_path'])
        assert ('partial_tail' in entry) == entry['path'].endswith('.jsonl')
    # The transcript opens with records that carry no version.
    assert manifest['harness_versions'] == ['2.1.296']


def test_harness_versions_lists_only_carried_versions_when_the_first_record_carries_none(roots, copier):
    s = Src(roots[0])
    s.append([dequeue(), conv('user', version='2.1.300')])
    copier.reconcile()
    session = Session.objects.get(pk=s.id)
    assert session.harness_versions == ['2.1.300']
    assert harness.UNKNOWN_VERSION not in session.harness_versions


def test_one_record_of_each_class_is_archived_and_counted_against_the_first_records_version(roots, copier):
    harness_root, archive = roots
    s = Src(harness_root)
    lines = [line(conv('user', version='9.9.1')), line({'type': 'mode', 'mode': 'x'}),
             line({'type': 'brand-new-type', 'x': 1}), b'{not json\n']
    s.append(lines)
    before = counts_read().get('9.9.1', {'unknown': 0, 'unparseable': 0})
    copier.reconcile()

    assert (Path(archive) / s.transcript.relative_to(harness_root)).read_bytes() == b''.join(lines)
    classes = [harness.classify_line(raw.rstrip(b'\n')) for raw in lines]
    assert [c.cls == harness.CONVERSATIONAL for c in classes] == [True, False, False, False]
    assert [c.cls for c in classes] == ['conversational', 'bookkeeping', 'unknown', 'unparseable']
    counts = Session.objects.get(pk=s.id).record_counts['9.9.1']
    assert counts == {'conversational': 1, 'bookkeeping': 1, 'unknown': 1, 'unparseable': 1}
    after = counts_read()['9.9.1']
    assert after['unknown'] == before['unknown'] + 1
    assert after['unparseable'] == before['unparseable'] + 1


def test_an_envelope_missing_a_field_is_unknown():
    record = conv('assistant')
    del record['parentUuid']
    assert harness.classify_line(json.dumps(record).encode()).cls == 'unknown'


def test_an_unparseable_first_line_is_counted_against_unknown(roots, copier):
    s = Src(roots[0])
    s.append([b'garbage\n', conv('user')])
    copier.reconcile()
    counts = Session.objects.get(pk=s.id).record_counts
    assert counts['unknown']['unparseable'] == 1
    assert counts['2.1.296']['unparseable'] == 0


def test_an_unparseable_line_after_a_versionless_record_takes_the_last_version_named(roots, copier):
    s = Src(roots[0])
    s.append([conv('user', version='3.0.0'), cost_state(), b'oops\n'])
    copier.reconcile()
    counts = Session.objects.get(pk=s.id).record_counts
    assert counts['3.0.0']['unparseable'] == 1
    assert 'unknown' not in counts


def test_only_jsonl_transcripts_are_classified(roots, copier):
    s = Src(roots[0])
    s.append([enqueue(), conv('user')])
    s.append([conv('assistant')], path=s.sidecar('subagents', 'agent-b.jsonl'))
    s.write('subagents/agent-b.meta.json', b'{"not":"a record"}')
    s.write('tool-results/t.txt', b'line one\nline two\n{"type":"user"}\n')
    copier.reconcile()
    counts = Session.objects.get(pk=s.id).record_counts
    total = sum(sum(per.values()) for per in counts.values())
    assert total == 3  # enqueue + user in the transcript, assistant in the subagent's


def test_the_classifier_and_run_records_live_in_the_harness_module():
    assert harness.is_prompt_record(enqueue())
    assert not harness.is_prompt_record(dequeue())
    assert not harness.is_prompt_record(enqueue('<task-notification>done</task-notification>'))
    assert not harness.is_prompt_record(enqueue('<agent-message from="x">report</agent-message>'))
    assert harness.is_prompt_record(enqueue('<cross-session-message from="y">do it</cross-session-message>'))
    assert harness.is_end_record(cost_state())
