"""Corpus Indexing REQ-01 (every class indexed, and a class not yet
indexed is observable), REQ-02 (every unit's reference resolves to its
source and matches it), REQ-08 (one stamped scope per unit), against the
index itself. Retrieval-posed Acceptance steps run through track 4's route
later (build brief §4)."""

import hashlib
import subprocess
import urllib.parse
import urllib.request

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from extraction.models import ExtractionRecord
from index import render
from index.models import CLASSES, PROJECT_CLASSES, IndexUnit
from index.status import index_status
from tests.index_support import (  # noqa: F401 - fixtures
    FakeCore, archive_api, asst, chained, checkout, core, extraction, index_once, prompt, redis_client,
    resolve_session_unit, search, text_block, units,
)
from tests.support import Session as Src, cost_state, enqueue

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def seeded(core, redis_client, archive_api, checkout, roots, copier):
    """One distinctive fact in each of the seven classes, plus the platform
    corpus (the fixture's checkout)."""
    harness, _ = roots
    spec_id, _ = extraction(text='# Spec\n\nREQ-01: the quokka gateway retries three times.\n')
    library_id, _ = extraction(text='Invoice ledger: the walrus budget closes on Fridays.\n')
    item = core.work_item('Calibrate the narwhal sensor', 'The narwhal sensor drifts in cold rooms.',
                          history=[('status', 'todo', 'in_progress')], spec_artifact=spec_id)
    core.comment(item['id'], 'The pelican cache was the culprit.')
    core.delivery(item['id'], 'commit', 'abc1234 fix the axolotl timeout')
    core.a2a_task(['Requesting review of the ocelot migration.'], work_item_id=item['id'])
    s = Src(harness)
    s.append([enqueue()] + chained(prompt('Investigate the flamingo outage.'),
                                  asst('m1', text_block('The flamingo outage came from a full disk.'))) + [cost_state()])
    notes = s.notes_dir()
    (notes / 'MEMORY.md').write_bytes(b'# Notes\n\nThe heron deploy needs a manual approval.\n')
    copier.reconcile()
    return {'item': item, 'spec': spec_id, 'library': library_id, 'session': s}


def test_req01_every_class_is_indexed_and_each_fact_is_found_in_its_own_class(seeded):
    index_once()
    facts = {'specification': 'quokka gateway', 'library_file': 'walrus budget', 'work_item': 'narwhal sensor',
             'work_item_comment': 'pelican cache', 'delivery_artifact': 'axolotl timeout',
             'a2a_message': 'ocelot migration', 'session': 'flamingo outage', 'platform_document': 'aurora valve'}
    for source_class, words in facts.items():
        found = search(words)
        assert found, f'no unit holds {words!r}'
        assert {u.source_class for u in found} == {source_class}, words
    # The note is a session-class unit too (REQ-01's session bullet; REQ-10).
    assert {u.source_class for u in search('heron deploy')} == {'session'}
    assert {u.source_class for u in units()} == set(CLASSES)


def test_req01_a_class_not_yet_indexed_is_observable_not_an_empty_result(seeded):
    before = index_status()
    assert before['incomplete']
    for name in ('core:work_items', 'core:comments', 'core:delivery_artifacts', 'core:a2a_tasks',
                 'memory:sessions', 'memory:notes', 'library', 'platform'):
        assert f'{name}: not indexed yet' in before['reasons']
    index_once()
    assert index_status()['reasons'] == []


def test_req02_every_units_reference_resolves_to_its_source_and_matches_it(seeded, core, archive_api, checkout):
    index_once()
    records = core.snapshot()
    checked = set()
    for unit in units():
        ref = unit.reference
        if unit.source_class == 'work_item':
            sections = dict(render.work_item(records['work_items'][ref['workItemId']]))
            source = sections[ref['section']]
        elif unit.source_class == 'work_item_comment':
            source = render.comment(records['comments'][ref['commentId']])
        elif unit.source_class == 'delivery_artifact':
            source = render.delivery_artifact(records['delivery'][ref['deliveryArtifactId']])
        elif unit.source_class == 'a2a_message':
            message = next(m for m in records['a2a'][ref['taskId']]['messages'] if m['messageId'] == ref['messageId'])
            source = render.a2a_message(message)
        elif unit.source_class in ('specification', 'library_file'):
            record = ExtractionRecord.objects.get(artifact_id=ref['artifactId'])
            # REQ-02: the extraction's stamp, the currency key of its REQ-03.
            assert ref['stamp'] == {'updatedAt': record.source_updated_at.isoformat(),
                                    'sha256': record.source_sha256}
            source = record.text
            assert ref['lineStart'] == source[:ref['offset']].count('\n') + 1
        elif unit.source_class == 'session' and 'turn' in ref:
            resolved, data = resolve_session_unit(archive_api, unit)
            assert resolved == unit.text
            lines = data.split(b'\n')
            assert all(b'"uuid"' in lines[n - 1] for n in ref['lines'])
            assert ref['lineStart'] == min(ref['lines']) and ref['lineEnd'] == max(ref['lines'])
            checked.add('session')
            continue
        elif unit.source_class == 'session':
            query = urllib.parse.urlencode({'path': ref['archivePath']})
            with urllib.request.urlopen(f'{archive_api}/notes/file?{query}') as response:
                data = response.read()
            # REQ-02: the digest of the archived copy it was built from.
            assert ref['digest'] == 'sha256:' + hashlib.sha256(data).hexdigest()
            source = data.decode()
            checked.add('note')
        else:
            shown = subprocess.run(['git', '-C', str(checkout), 'show', f"{ref['commit']}:{ref['path']}"],
                                   check=True, capture_output=True, text=True).stdout
            source = shown
        assert source[ref['offset']:ref['end']] == unit.text, unit.source_key
        checked.add(unit.source_class)
    assert checked >= set(CLASSES) | {'note'}


def test_req08_every_unit_carries_exactly_one_scope_set_by_where_it_came_from(seeded):
    index_once()
    all_units = units()
    assert all_units
    for unit in all_units:
        expected = 'platform' if unit.source_class == 'platform_document' else 'project'
        assert unit.scope == expected, unit.source_key
    # A query filtered to each value returns only units carrying it.
    for scope in ('project', 'platform'):
        found = search('the', scope=scope) + search('aurora valve', scope=scope) + search('narwhal', scope=scope)
        assert found and {u.scope for u in found} == {scope}
    assert search('aurora valve', scope='project') == []
    assert search('narwhal sensor', scope='platform') == []


def _unit(**overrides):
    fields = {'source_class': 'work_item', 'scope': 'project', 'source_key': 'work_item:x',
              'reference': {'workItemId': 'x', 'section': 'fields', 'offset': 0, 'end': 4}, 'text': 'text',
              'indexed_at': timezone.now()}
    fields.update(overrides)
    with transaction.atomic():
        IndexUnit.objects.create(**fields)


@pytest.mark.parametrize('overrides', [
    {'scope': 'platform'},                                              # a project class stamped platform
    {'source_class': 'platform_document', 'scope': 'project',
     'reference': {'commit': 'c', 'path': 'p', 'lineStart': 1, 'lineEnd': 1, 'offset': 0, 'end': 4}},
    {'scope': 'tenant-b'},                                              # a third scope value
    {'source_class': 'decision_record'},                                # not a class
    {'text': ''},
    {'reference': {'workItemId': 'x'}},                                 # no location
    {'source_class': 'library_file', 'reference': {'artifactId': 'a', 'stamp': {'updatedAt': 't'},
                                                    'lineStart': 1, 'lineEnd': 1, 'offset': 0, 'end': 4}},
    {'source_class': 'session', 'reference': {'sessionId': 's', 'file': 'f', 'turn': 't', 'lines': [1],
                                               'lineStart': 1, 'lineEnd': 1, 'offset': 0, 'end': 4,
                                               'parentSessionId': 's'}},  # subagent without its tool call
])
def test_req02_req08_the_database_refuses_a_unit_without_a_resolvable_reference_or_a_valid_scope(overrides):
    with pytest.raises(IntegrityError):
        _unit(**overrides)


def test_the_valid_form_of_that_unit_is_accepted():
    _unit()
    assert IndexUnit.objects.count() == 1
    assert set(PROJECT_CLASSES) | {'platform_document'} == set(CLASSES)
