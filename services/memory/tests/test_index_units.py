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
from index.embedding import MODEL
from index.models import CLASSES, PROJECT_CLASSES, IndexUnit
from index.status import index_status
from tests.index_support import (  # noqa: F401 - fixtures
    archive_api, checkout, core, index_once, redis_client, resolve_session_unit, search, seeded, units,
)
from tests.retrieval_support import read_source, status_core, unexplained  # noqa: F401 - fixture

pytestmark = pytest.mark.django_db(transaction=True)


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


def test_req02_every_units_reference_resolves_to_its_source_and_matches_it(seeded, core, status_core, archive_api,
                                                                         checkout):
    from retrieval import sources
    index_once()
    checked = set()
    for unit in units():
        ref = unit.reference
        if unit.source_class in ('work_item', 'work_item_comment', 'delivery_artifact', 'a2a_message'):
            # A Core record: follow each named field into the JSON Core's own read
            # returns, and confirm the unit's text is those fields' values there
            # (Knowledge Retrieval REQ-04). Memory's rendering is not the source.
            body = read_source(core, sources.address(unit.source_class, ref))
            assert ref['fields'] and unexplained(unit.text, ref['fields'], body) == [], unit.source_key
            checked.add(unit.source_class)
            continue
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


def test_req02_a_delivery_artifacts_url_words_are_searchable_but_not_in_its_text(seeded, core):
    url = 'https://git.example.com/org/zebrafish-api/commit/4242'
    core.delivery(seeded['item']['id'], 'commit', url)
    index_once()
    found = search('zebrafish api org')
    assert [u.source_class for u in found] == ['delivery_artifact']
    # The unit's text is the record's fields and labels only: no line of words added to it.
    assert found[0].text == f'Delivery artifact (commit): {url}\n'
    # The second, search-only reading is a delivery artifact's alone.
    core.comment(seeded['item']['id'], f'See {url.replace("zebrafish", "manatee")}')
    index_once()
    assert search('manatee') == []


def test_kr_req05_a_turn_whose_first_record_has_no_time_is_dated_by_its_first_record_that_has_one():
    import json

    from index import sessions
    from tests.index_support import asst, chained, prompt, text_block
    first = prompt('Investigate the gannet outage.')
    del first['timestamp']
    reply = asst('m1', text_block('The gannet outage came from a full disk.'))
    reply['timestamp'] = '2026-10-12T12:05:00Z'
    data = ''.join(json.dumps(r) + '\n' for r in chained(first, reply)).encode()
    turn_units = sessions.turn_units(data, upto_line=None)
    assert turn_units and {u.written_at for u in turn_units} == {'2026-10-12T12:05:00Z'}


def _unit(**overrides):
    fields = {'source_class': 'work_item', 'scope': 'project', 'source_key': 'work_item:x',
              'reference': {'workItemId': 'x', 'section': 'fields', 'offset': 0, 'end': 4,
                            'fields': [{'path': ['description'], 'span': [0, 4], 'valueOffset': 0}]},
              'text': 'text',
              # A whole vector, so each refusal below is the reference's or the scope's.
              'embedding': bytes(4 * MODEL.dim),
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
    # REQ-04: a Core record's unit that names no field of Core's read
    {'reference': {'workItemId': 'x', 'section': 'fields', 'offset': 0, 'end': 4}},
    {'reference': {'workItemId': 'x', 'section': 'fields', 'offset': 0, 'end': 4, 'fields': []}},
    {'source_class': 'work_item_comment', 'reference': {'commentId': 'c', 'workItemId': 'x', 'offset': 0, 'end': 4}},
    {'source_class': 'delivery_artifact',
     'reference': {'deliveryArtifactId': 'd', 'workItemId': 'x', 'offset': 0, 'end': 4, 'fields': [{}]}},
    {'source_class': 'a2a_message', 'reference': {'taskId': 't', 'messageId': 'm', 'offset': 0, 'end': 4,
                                                   'fields': {'path': ['messages']}}},
    {'source_class': 'library_file', 'reference': {'artifactId': 'a', 'stamp': {'updatedAt': 't'},
                                                    'lineStart': 1, 'lineEnd': 1, 'offset': 0, 'end': 4}},
    {'source_class': 'session', 'reference': {'sessionId': 's', 'file': 'f', 'turn': 't', 'lines': [1],
                                               'lineStart': 1, 'lineEnd': 1, 'offset': 0, 'end': 4,
                                               'parentSessionId': 's'}},  # subagent without its tool call
    {'source_class': 'session', 'reference': {'project': 'p', 'path': 'memory/MEMORY.md', 'digest': 'sha256:d',
                                               'lineStart': 1, 'lineEnd': 1, 'offset': 0, 'end': 4}},
    # a note without the archived copy its passage resolves through
])
def test_req02_req08_the_database_refuses_a_unit_without_a_resolvable_reference_or_a_valid_scope(overrides):
    with pytest.raises(IntegrityError):
        _unit(**overrides)


def test_the_valid_form_of_that_unit_is_accepted():
    _unit()
    assert IndexUnit.objects.count() == 1
    assert set(PROJECT_CLASSES) | {'platform_document'} == set(CLASSES)


def _historical(app_label, migration, model):
    """A model as it stood at ``migration``, for writing rows a later
    migration must deal with."""
    from django.db import connection
    from django.db.migrations.loader import MigrationLoader
    return MigrationLoader(connection).project_state((app_label, migration)).apps.get_model(app_label, model)


def test_req04_migration_0003_removes_core_units_indexed_before_they_named_their_fields():
    """A unit indexed before Core passages named their fields cannot satisfy
    the tightened CHECK, so migration 0003 removes it, with its source row,
    and drops the Core cursors so the indexer reads those classes again."""
    from django.core.management import call_command

    from index.models import IndexCursor, IndexSource
    call_command('migrate', 'index', '0002', verbosity=0)
    try:
        Unit = _historical('index', '0002_search_words_and_note_archive_path', 'IndexUnit')
        now = timezone.now()
        old = {'workItemId': 'x', 'section': 'fields', 'offset': 0, 'end': 4}
        Unit.objects.create(source_class='work_item', scope='project', source_key='work_item:x',
                            reference=old, text='text', indexed_at=now)
        Unit.objects.create(source_class='platform_document', scope='platform', source_key='platform:p',
                            reference={'commit': 'c', 'path': 'p', 'lineStart': 1, 'lineEnd': 1,
                                       'offset': 0, 'end': 4}, text='text', indexed_at=now)
        IndexSource.objects.create(source_key='work_item:x', source_class='work_item', stamp={}, indexed_at=now)
        IndexCursor.objects.create(name='core:work_items', cursor='42', caught_up=True)
        IndexCursor.objects.create(name='platform', caught_up=True)
        call_command('migrate', 'index', '0003', verbosity=0)
        assert list(IndexUnit.objects.values_list('source_key', flat=True)) == ['platform:p']
        assert not IndexSource.objects.filter(source_key='work_item:x').exists()
        assert list(IndexCursor.objects.values_list('name', flat=True)) == ['platform']
        assert 'core:work_items: not indexed yet' in index_status()['reasons']
    finally:
        call_command('migrate', 'index', verbosity=0)


def test_migration_0004_empties_the_index_so_every_unit_is_written_again_with_its_embedding():
    """Track 5: a unit indexed before embeddings has none, and the CHECK
    refuses one without, so migration 0004 empties the index as a rebuild
    does (Corpus Indexing REQ-03), and every class reads "not indexed yet"
    until the indexer has read it again."""
    from django.core.management import call_command

    from index.models import IndexCursor, IndexSource, IndexState
    call_command('migrate', 'index', '0003', verbosity=0)
    try:
        Unit = _historical('index', '0003_core_passages_name_their_fields', 'IndexUnit')
        now = timezone.now()
        Unit.objects.create(source_class='platform_document', scope='platform', source_key='platform:p',
                            reference={'commit': 'c', 'path': 'p', 'lineStart': 1, 'lineEnd': 1,
                                       'offset': 0, 'end': 4}, text='text', indexed_at=now)
        IndexSource.objects.create(source_key='platform:p', source_class='platform_document', stamp={},
                                   indexed_at=now)
        IndexCursor.objects.create(name='platform', caught_up=True)
        IndexState.objects.create(pk=1, platform_commit='c')
    finally:
        call_command('migrate', 'index', verbosity=0)
    assert not IndexUnit.objects.exists() and not IndexSource.objects.exists()
    assert not IndexCursor.objects.exists() and not IndexState.objects.exists()
    assert 'platform: not indexed yet' in index_status()['reasons']
