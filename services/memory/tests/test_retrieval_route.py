"""Knowledge Retrieval through its real route, over an index the real
indexer filled from the seeded records (tests/index_support.py ``seeded``):
REQ-01 … REQ-07 and the Acceptance steps track 3 handed to this track
(PRD AC-05, AC-07, AC-08, AC-10 and AC-04's retrieval half)."""

import hashlib
import json
import subprocess
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from django.core.management import call_command

from archive.models import Note, Session, SessionFile
from extraction.models import ExtractionRecord
from index.models import IndexSource, IndexUnit
from retrieval.models import QueryLog
from tests.index_support import (  # noqa: F401 - fixtures
    archive_api, checkout, core, extraction, index_once, redis_client, resolve_session_unit, seeded, stream,
    upload_event, work_item_event,
)
from tests.retrieval_support import (  # noqa: F401 - fixtures
    ask, follow, passages, read_source, status_core, unexplained,
)

pytestmark = pytest.mark.django_db(transaction=True)

# One plain question per seeded fact; none names its source or its id (AC-05).
FACT_QUESTIONS = [
    ('How often does the quokka gateway retry?', 'specification'),
    ('When does the walrus budget close?', 'library_file'),
    ('Which sensor drifts in cold rooms?', 'work_item'),
    ('What was the culprit about the cache?', 'work_item_comment'),
    ('Which commit fixed the axolotl timeout?', 'delivery_artifact'),
    ('Who asked for a review of the ocelot migration?', 'a2a_message'),
    ('What caused the flamingo outage?', 'session'),
    ('What does the heron deploy need?', 'session'),
    ('How is the aurora valve reset?', 'platform_document'),
]


@pytest.fixture
def indexed(seeded, status_core, core):
    index_once()
    # Core's artifact records, current against the extractions the index was built from.
    for record in ExtractionRecord.objects.all():
        status_core.artifacts[str(record.artifact_id)] = record.source_updated_at.isoformat()
    return seeded


def top(client, question, **fields):
    found = passages(client, question, **fields)
    assert found, question
    return found[0]


# --- AC-05: each class's fact, found by a question that names neither its source nor its id -----

def test_ac05_each_of_the_seven_classes_fact_is_retrieved_from_its_own_class(client, indexed):
    for question, source_class in FACT_QUESTIONS:
        assert top(client, question)['class'] == source_class, question
    # The note is in the session class too, and the platform pair carries `platform`.
    assert top(client, 'What does the heron deploy need?')['reference'].get('path')


def test_ac05_a_delivery_artifact_with_a_url_reference_is_found_by_words_inside_the_url(client, indexed, core):
    core.delivery(indexed['item']['id'], 'commit', 'https://git.example.com/org/zebrafish-api/commit/4242')
    index_once()
    found = top(client, 'Which zebrafish api org change landed?')
    assert found['class'] == 'delivery_artifact'
    assert 'zebrafish' in found['text'].lower()


# --- REQ-01: one interface for a person and a test harness -----------------------

def test_req01_a_person_and_a_test_harness_get_results_of_the_same_shape_from_the_same_edge(client, indexed):
    question = 'What caused the flamingo outage?'
    person = client.get('/retrieve', {'question': question})
    harness = client.post('/retrieve', data=json.dumps({'question': question}), content_type='application/json')
    assert person.status_code == harness.status_code == 200
    a, b = person.json(), harness.json()
    assert set(a) == set(b)
    assert [p['unitId'] for p in a['passages']] == [p['unitId'] for p in b['passages']] and a['passages']
    assert set(a['passages'][0]) == set(b['passages'][0])
    assert a['queryId'] != b['queryId']


def test_req01_a_request_that_is_not_a_question_is_refused(client, indexed):
    assert ask(client, '   ')[0] == 400
    assert client.post('/retrieve', data='[1]', content_type='application/json').status_code == 400
    assert client.post('/retrieve', data='nope', content_type='application/json').status_code == 400
    assert client.get('/retrieve').status_code == 400
    assert ask(client, 'x', limit=0)[0] == 400 and ask(client, 'x', limit='3')[0] == 400
    assert ask(client, 'x', sessionWeight=-1)[0] == 400 and ask(client, 'x', sessionCap=-1)[0] == 400
    assert client.get('/retrieve', {'question': 'x', 'limit': 'many'}).status_code == 400
    assert client.delete('/retrieve').status_code == 405


# --- REQ-06 / AC-08: two scopes, no cross-project path -------------------------------

def test_ac08_no_declaration_searches_both_each_value_narrows_and_a_third_is_refused(client, indexed):
    question = 'Which is it: the quokka gateway retry, or the aurora valve reset?'
    both = passages(client, question)
    assert {p['scope'] for p in both} == {'project', 'platform'}
    assert {p['class'] for p in both if p['scope'] == 'platform'} == {'platform_document'}
    assert {p['scope'] for p in passages(client, question, scope='project')} == {'project'}
    assert {p['scope'] for p in passages(client, question, scope='platform')} == {'platform'}
    status, body = ask(client, question, scope='some-other-project')
    assert status == 400 and 'some-other-project' in body['error'] and 'passages' not in body
    assert ask(client, question, scope='')[0] == 400
    assert ask(client, question, scope='PROJECT')[0] == 400


def test_req06_the_request_has_no_field_that_reaches_another_instance(client, indexed):
    for field in ('project', 'tenant', 'instance', 'projectId', 'instanceId'):
        status, body = ask(client, 'What caused the flamingo outage?', **{field: 'other'})
        assert status == 400 and field in body['error'], field
    assert client.get('/retrieve', {'question': 'q', 'project': 'other'}).status_code == 400


def test_req06_the_declared_scope_is_stamped_on_every_passage_and_recorded(client, indexed):
    status, body = ask(client, 'What caused the flamingo outage?', scope='project')
    assert body['scope'] == 'project' and body['scopesSearched'] == ['project']
    assert all(p['scope'] == 'project' for p in body['passages'])
    entry = QueryLog.objects.get(pk=body['queryId'])
    assert entry.scope_declared == 'project'
    none = ask(client, 'What caused the flamingo outage?')[1]
    assert none['scope'] is None and none['scopesSearched'] == ['project', 'platform']
    assert QueryLog.objects.get(pk=none['queryId']).scope_declared is None


# --- REQ-03: the query and exactly what it returned are recoverable ------------------

def test_req03_a_query_and_its_exact_passages_are_recovered_afterward(client, indexed):
    status, body = ask(client, 'What caused the flamingo outage?', limit=3)
    recovered = client.get(f"/retrieve/queries/{body['queryId']}").json()
    assert recovered['question'] == 'What caused the flamingo outage?'
    assert recovered['response'] == json.loads(json.dumps(body))
    assert recovered['response']['passages'] == body['passages']
    assert [(p['unitId'], p['scope']) for p in recovered['response']['passages']]
    assert recovered['indexIncomplete'] is body['incomplete'] is False
    assert recovered['parameters']['limit'] == 3
    assert client.get('/retrieve/queries/999999').status_code == 404


# --- REQ-02: retrieval changes no authoritative record; the log is not corpus ---------

def _authoritative(core, status_core):
    """Every record retrieval might touch, with the exclusions the build
    brief's I-25 names: Core's access_log (``status_core.access_log``,
    written by REQ-05's query-time reads), Memory's read log (the container
    log, not a table) and the query log (``retrieval_query_log``) are not
    authoritative records and are left out. Memory's own derived index is
    included, since retrieval must not write it either."""
    return {
        'core': core.snapshot(),
        'extraction': sorted(ExtractionRecord.objects.values_list('artifact_id', 'source_updated_at',
                                                                  'source_sha256', 'outcome', 'text')),
        'sessions': sorted(Session.objects.values_list('session_id', 'status', 'change_seq')),
        'files': sorted(SessionFile.objects.values_list('path', 'digest', 'bytes_read')),
        'notes': sorted(Note.objects.values_list('path', 'digest', 'removed')),
        'index': sorted(IndexUnit.objects.values_list('source_key', 'position', 'text', 'current', 'indexed_at')),
        'index_sources': sorted(IndexSource.objects.values_list('source_key', 'stamp', 'indexed_at')),
    }


def test_req02_retrieval_changes_no_authoritative_record_and_its_log_is_not_returned(client, indexed, core,
                                                                                   status_core):
    before = _authoritative(core, status_core)
    for question, _ in FACT_QUESTIONS:
        assert passages(client, question)
    assert _authoritative(core, status_core) == before
    # The exclusion is real: the reads wrote access_log rows in Core, and the query log grew.
    assert status_core.access_log and QueryLog.objects.count() == len(FACT_QUESTIONS)
    assert {actor for _, actor, _ in status_core.access_log} == {'memory-retrieval'}
    # A logged question is never itself a passage.
    ask(client, 'zyxwv unmistakable logged phrase')
    assert passages(client, 'zyxwv unmistakable logged phrase') == []
    assert not IndexUnit.objects.filter(text__icontains='zyxwv').exists()


# --- REQ-04 / AC-07: every passage resolves to its source -----------------------------

CORE_CLASSES = ('work_item', 'work_item_comment', 'delivery_artifact', 'a2a_message')


def _check_core_passage(passage, core):
    """REQ-04 for a Core record's passage: its reference names the fields of
    the JSON its ``read`` returns, and the passage is drawn from those
    fields' values, read from the same Core stub the indexer read."""
    fields = passage['reference']['fields']
    assert fields and passage['source']['location'] == {'fields': fields}
    body = read_source(core, passage['source'])
    assert unexplained(passage['text'], fields, body) == [], passage
    return body


def test_ac07_the_field_check_catches_text_its_fields_do_not_hold():
    body = {'artifacts': [{'id': 'd1', 'artifact_type': 'commit',
                           'reference': 'https://git.example.com/org/zebrafish-api/commit/4242'}]}
    at, url = ['artifacts', {'id': 'd1'}], body['artifacts'][0]['reference']
    text = f'Delivery artifact (commit): {url}\n'
    fields = [{'path': at + ['artifact_type'], 'span': [19, 25], 'valueOffset': 0},
              {'path': at + ['reference'], 'span': [28, 28 + len(url)], 'valueOffset': 0}]
    assert unexplained(text, fields, body) == []
    # Words the record holds nowhere, outside every named field.
    added = text + 'https git example com org zebrafish api commit 4242\n'
    assert unexplained(added, fields, body) == [('outside', '\nhttps git example com org zebrafish api commit 4242\n')]
    # A field named that does not hold the text at its span.
    assert unexplained(text, [fields[0], {**fields[1], 'valueOffset': 1}], body)
    assert unexplained('Delivery artifact (build): x\n', fields[:1], body)
    # A field left unnamed leaves its value unexplained.
    assert unexplained(text, fields[:1], body)


def test_req04_each_core_class_passage_names_the_fields_of_its_read_that_hold_its_text(client, indexed, core):
    questions = {'work_item': 'Which sensor drifts in cold rooms?',
                 'work_item_comment': 'What was the culprit about the cache?',
                 'delivery_artifact': 'Which commit fixed the axolotl timeout?',
                 'a2a_message': 'Who asked for a review of the ocelot migration?'}
    item_id = indexed['item']['id']
    for source_class, question in questions.items():
        passage = top(client, question)
        assert passage['class'] == source_class, question
        body = _check_core_passage(passage, core)
        paths = [f['path'] for f in passage['reference']['fields']]
        if source_class == 'work_item':
            assert passage['source']['read'] == f'/work-items/{item_id}?full=true'
            assert ['description'] in paths and ['display_name'] in paths
        elif source_class == 'work_item_comment':
            comment_id = passage['reference']['commentId']
            assert ['comments', {'id': comment_id}, 'body'] in paths
            assert 'pelican cache' in follow(body, ['comments', {'id': comment_id}, 'body'])
        elif source_class == 'delivery_artifact':
            artifact_id = passage['reference']['deliveryArtifactId']
            assert ['artifacts', {'id': artifact_id}, 'reference'] in paths
        else:
            message_id = passage['reference']['messageId']
            assert passage['source']['read'] == f"/a2a/tasks/{passage['reference']['taskId']}"
            assert ['messages', {'messageId': message_id}, 'parts', 0, 'text'] in paths


def test_req04_a_work_item_passage_spanning_several_fields_names_each(client, indexed, core):
    record = core.records['work_items'][indexed['item']['id']]
    record['storyDetail'] = {'work_item_id': record['id'], 'behavior': 'The tapir scale reports grams.',
                             'acceptance_criteria': 'Weighing a tapir twice gives the same grams.',
                             'constraints': None, 'edge_cases': None, 'out_of_scope': None,
                             'value_hypothesis': None, 'test_measurement': None}
    core.put('work_items', record)
    index_once()
    passage = top(client, 'How does the tapir scale report grams?')
    assert passage['class'] == 'work_item' and passage['reference']['section'] == 'fields'
    _check_core_passage(passage, core)
    paths = [f['path'] for f in passage['reference']['fields']]
    assert ['storyDetail', 'behavior'] in paths and ['storyDetail', 'acceptance_criteria'] in paths
    assert ['description'] in paths and len(paths) >= 5


def test_req04_a_history_passage_names_the_history_entry_and_its_fields(client, indexed, core):
    record = core.records['work_items'][indexed['item']['id']]
    record['history'].append({'id': 'hist-capybara', 'work_item_id': record['id'], 'field': 'assignee_agent_id',
                              'old_value': None, 'new_value': 'capybara-wrangler', 'actor': 'scrummaster',
                              'occurred_at': '2026-10-12T09:00:00+00:00'})
    core.put('work_items', record)
    index_once()
    passage = top(client, 'Who was the capybara wrangler?')
    assert passage['class'] == 'work_item' and passage['reference']['section'] == 'history'
    body = _check_core_passage(passage, core)
    paths = [f['path'] for f in passage['reference']['fields']]
    entry = ['history', {'id': 'hist-capybara'}]
    assert all(entry + [key] in paths for key in ('occurred_at', 'actor', 'field', 'new_value'))
    # A missing old value is rendered "(none)", which is no field.
    assert entry + ['old_value'] not in paths
    assert follow(body, entry + ['new_value']) == 'capybara-wrangler'
    # The entry before it, with an old value, is named too.
    first = record['history'][0]['id']
    assert ['history', {'id': first}, 'old_value'] in paths


def test_req04_an_a2a_passage_names_each_part_and_artifact_it_holds(client, indexed, core):
    task = core.a2a_task(['Please benchmark the okapi parser.'])
    message = task['messages'][0]
    message['parts'].append({'kind': 'data', 'data': {'okapiRuns': 3}})
    message['artifacts'] = [{'artifactId': 'art-okapi', 'name': 'okapi-results', 'parts': [], 'messageId':
                             message['messageId'], 'position': 0, 'referenceArtifactId': None, 'metadata': None,
                             'recordedAt': message['recordedAt'],
                             'artifact': {'artifactId': 'art-okapi', 'name': 'okapi-results',
                                          'parts': [{'kind': 'text', 'text': 'The okapi parser ran in 40 ms.'}]}}]
    core.put('a2a', task)
    index_once()
    passage = top(client, 'How fast was the okapi parser benchmark?')
    assert passage['class'] == 'a2a_message'
    _check_core_passage(passage, core)
    at = ['messages', {'messageId': message['messageId']}]
    paths = [f['path'] for f in passage['reference']['fields']]
    assert at + ['parts', 0, 'text'] in paths and at + ['parts', 1, 'data'] in paths
    assert at + ['artifacts', {'artifactId': 'art-okapi'}, 'artifact', 'parts', 0, 'text'] in paths
    assert at + ['role'] in paths and at + ['direction'] in paths


def _source_text(ref, source_class, core, archive_api, checkout):
    if source_class in ('specification', 'library_file'):
        return ExtractionRecord.objects.get(artifact_id=ref['artifactId']).text
    if source_class == 'session' and 'turn' in ref:
        text, _ = resolve_session_unit(archive_api, SimpleNamespace(reference=ref))
        return text
    if source_class == 'session':
        query = urllib.parse.urlencode({'path': ref['archivePath']})
        with urllib.request.urlopen(f'{archive_api}/notes/file?{query}') as response:
            data = response.read()
        assert ref['digest'] == 'sha256:' + hashlib.sha256(data).hexdigest()
        return data.decode()
    return subprocess.run(['git', '-C', str(checkout), 'show', f"{ref['commit']}:{ref['path']}"], check=True,
                          capture_output=True, text=True).stdout


def test_ac07_every_passage_in_a_result_set_resolves_to_its_source_and_matches_it(client, indexed, core,
                                                                                   archive_api, checkout):
    # A delivery artifact whose reference is a URL: its passage must hold the URL, not words added to it.
    core.delivery(indexed['item']['id'], 'commit', 'https://git.example.com/org/zebrafish-api/commit/4242')
    index_once()
    questions = [q for q, _ in FACT_QUESTIONS] + ['Which zebrafish api org change landed?']
    resolved = set()
    for question in questions:
        for passage in passages(client, question, limit=5):
            ref = passage['reference']
            if passage['class'] in CORE_CLASSES:
                # Core's records: the passage is drawn from the fields its reference names,
                # in the JSON its read returns.
                _check_core_passage(passage, core)
                assert passage['source']['owner'] == 'core'
                resolved.add(passage['class'])
                continue
            source = _source_text(ref, passage['class'], core, archive_api, checkout)
            if passage['class'] == 'session' and 'turn' in ref:
                assert source == passage['text']
            else:
                assert source[ref['offset']:ref['end']] == passage['text'], (question, passage['class'])
            assert passage['source']['owner'] and passage['source']['read'] and passage['source']['location']
            resolved.add(passage['class'])
    assert resolved >= {'specification', 'library_file', 'work_item', 'work_item_comment', 'delivery_artifact',
                        'a2a_message', 'session', 'platform_document'}


def test_req04_a_passage_whose_reference_cannot_resolve_is_not_returned(client, indexed, monkeypatch):
    from retrieval import ranking
    real = ranking.rank

    def broken(*args, **kwargs):
        found = real(*args, **kwargs)
        found[0].reference = {}
        return found

    monkeypatch.setattr(ranking, 'rank', broken)
    status, body = ask(client, 'What caused the flamingo outage?')
    assert body['withheld'][0]['unitId'] not in [p['unitId'] for p in body['passages']]
    assert all(p['reference'] for p in body['passages'])
    assert body['withheld'][0]['reason'] == 'its reference does not resolve to a source'


def test_req04_a_note_passage_without_its_archived_copy_is_not_returned(client, indexed, monkeypatch):
    from retrieval import ranking
    real = ranking.rank
    stripped = []

    def without_archive_path(*args, **kwargs):
        found = real(*args, **kwargs)
        for candidate in found:
            if candidate.source_class == 'session' and 'archivePath' in candidate.reference:
                candidate.reference = {k: v for k, v in candidate.reference.items() if k != 'archivePath'}
                stripped.append(candidate.unit_id)
        return found

    monkeypatch.setattr(ranking, 'rank', without_archive_path)
    status, body = ask(client, 'What does the heron deploy need?')
    assert stripped
    assert {w['unitId'] for w in body['withheld']} >= set(stripped)
    assert not {p['unitId'] for p in body['passages']} & set(stripped)
    assert all(w['reason'] == 'its reference does not resolve to a source' for w in body['withheld'])


# --- REQ-05: derived, aged, and carrying the source's current status --------------------

def test_req05_every_passage_is_derived_dated_and_says_the_authoritative_record_governs(client, indexed):
    status, body = ask(client, 'What caused the flamingo outage?')
    assert 'source record governs' in body['notice']
    seen = set()
    for question, _ in FACT_QUESTIONS:
        for passage in passages(client, question, limit=5):
            assert passage['derived'] is True and passage['authoritative'] is False
            assert passage['writtenAt'], (question, passage['class'])
            assert isinstance(passage['ageSeconds'], int) and passage['writtenAtKnown'] is True
            seen.add(passage['class'])
    assert seen >= {'specification', 'library_file', 'work_item', 'work_item_comment', 'delivery_artifact',
                    'a2a_message', 'session', 'platform_document'}


def test_req05_a_platform_passage_is_dated_by_the_last_commit_to_touch_its_document(client, indexed, checkout):
    found = top(client, 'How is the aurora valve reset?')
    assert found['class'] == 'platform_document'
    committed = subprocess.run(['git', '-C', str(checkout), 'log', '-1', '--format=%cI', found['reference']['commit'],
                                '--', found['reference']['path']], check=True, capture_output=True,
                               text=True).stdout.strip()
    assert datetime.fromisoformat(found['writtenAt']) == datetime.fromisoformat(committed)


def test_req05_a_work_item_whose_status_changed_reports_its_current_status(client, indexed, core):
    item = indexed['item']
    assert top(client, 'Which sensor drifts in cold rooms?')['sourceStatus']['status'] == 'todo'
    core.put('work_items', {**core.records['work_items'][item['id']], 'status': 'done'})
    found = top(client, 'Which sensor drifts in cold rooms?')
    assert found['sourceStatus'] == {'kind': 'work_item', 'state': 'known', 'workItemId': item['id'],
                                     'status': 'done', 'updatedAt': found['sourceStatus']['updatedAt']}
    # The comment and delivery artifact carry their work item's status too.
    assert top(client, 'What was the culprit about the cache?')['sourceStatus']['status'] == 'done'
    # The index holds the old text until the indexer reads the change; the status is read now.
    assert 'todo' in IndexUnit.objects.get(pk=found['unitId']).text.lower()


def test_req05_an_a2a_message_reports_scrummasters_verdict_and_reason_or_that_it_has_none(client, indexed, core,
                                                                                       status_core):
    task_id = next(iter(core.records['a2a']))
    found = top(client, 'Who asked for a review of the ocelot migration?')
    assert found['sourceStatus']['verdictRecorded'] is False and found['sourceStatus']['verdict'] is None
    assert 'not yet recorded' in found['sourceStatus']['note']
    status_core.set_verdict(task_id, 0, 'refused', reason='the work item is already done')
    found = top(client, 'Who asked for a review of the ocelot migration?')
    assert found['sourceStatus']['verdict'] == 'refused'
    assert found['sourceStatus']['reason'] == 'the work item is already done'
    assert found['sourceStatus']['verdictRecorded'] is True
    status_core.set_verdict(task_id, 0, 'applied', outcome='completed')
    found = top(client, 'Who asked for a review of the ocelot migration?')
    assert (found['sourceStatus']['verdict'], found['sourceStatus']['outcome']) == ('applied', 'completed')


def test_req05_a_library_file_is_current_until_its_artifact_is_re_uploaded(client, indexed, status_core, core):
    record = ExtractionRecord.objects.get(artifact_id=indexed['library'])
    status_core.artifacts[str(indexed['library'])] = record.source_updated_at.isoformat()
    found = top(client, 'When does the walrus budget close?')
    assert found['sourceStatus']['current'] is True
    # Re-uploaded: the artifact's record moves on, the index has not followed yet.
    status_core.artifacts[str(indexed['library'])] = (record.source_updated_at + timedelta(minutes=1)).isoformat()
    status, body = ask(client, 'When does the walrus budget close?')
    assert body['passages'] == [] and body['withheld'][0]['class'] == 'library_file'
    assert 'not current' in body['withheld'][0]['reason']
    # Withheld is not "nothing relevant": a passage exists, it is just not current.
    assert body['nothingRelevant'] is False


def test_req05_core_unreachable_is_reported_not_hidden(client, indexed, status_core, core):
    core.available = False
    status, body = ask(client, 'Which sensor drifts in cold rooms?')
    assert body['statusUnavailable'] is True
    assert body['passages'][0]['sourceStatus']['state'] == 'unavailable'


def test_req05_a_source_deleted_in_core_reads_as_deleted(client, indexed, core):
    core.delete('work_items', indexed['item']['id'])
    found = top(client, 'Which sensor drifts in cold rooms?')
    assert found['sourceStatus']['state'] == 'deleted'


# --- AC-04 (retrieval half): a re-upload is followed, and the old text is never current ----

def test_ac04_after_a_re_upload_retrieval_returns_the_new_content_and_not_the_old(client, indexed, redis_client,
                                                                               status_core):
    artifact_id = str(indexed['library'])
    old = ExtractionRecord.objects.get(artifact_id=artifact_id)
    status_core.artifacts[artifact_id] = old.source_updated_at.isoformat()
    assert top(client, 'When does the walrus budget close?')['text'].startswith('Invoice ledger')

    new_at = old.source_updated_at + timedelta(minutes=5)
    status_core.artifacts[artifact_id] = new_at.isoformat()
    redis_client.xadd('aigang:artifacts:events', upload_event(artifact_id, new_at, action='replaced'))
    index_once()
    # Announced, not yet extracted: the old text is not current and the index says it may be incomplete.
    status, body = ask(client, 'When does the walrus budget close?')
    assert body['passages'] == [] and body['incomplete'] is True and body['nothingRelevant'] is False
    assert any('awaiting extraction' in reason for reason in body['incompleteReasons'])

    extraction(artifact_id, 'Invoice ledger: the walrus budget closes on Mondays now.\n', updated_at=new_at)
    index_once()
    found = passages(client, 'When does the walrus budget close?')
    assert 'Mondays' in found[0]['text'] and all('Fridays' not in p['text'] for p in found)
    assert found[0]['sourceStatus']['current'] is True
    assert ask(client, 'When does the walrus budget close?')[1]['incomplete'] is False


# --- REQ-07: "nothing relevant" is told apart from "not indexed yet" -------------------

def test_req07_lag_is_reported_and_clears_when_the_index_catches_up(client, seeded, status_core, core,
                                                                    redis_client):
    index_once()
    quiet = ask(client, 'Where is the unmistakable zeppelin hangar?')[1]
    assert quiet['incomplete'] is False and quiet['nothingRelevant'] is True and quiet['passages'] == []

    item = core.work_item('Survey the lynx trail near the zeppelin hangar')
    redis_client.xadd(stream(), work_item_event('work_item.created', item['id']))
    lagging = ask(client, 'Where is the unmistakable zeppelin hangar?')[1]
    assert lagging['incomplete'] is True and lagging['nothingRelevant'] is False
    assert any(stream() in reason for reason in lagging['incompleteReasons'])
    assert lagging['passages'] == []

    index_once()
    caught_up = ask(client, 'Where is the unmistakable zeppelin hangar?')[1]
    assert caught_up['incomplete'] is False and caught_up['passages']
    assert QueryLog.objects.get(pk=lagging['queryId']).index_incomplete is True
    assert QueryLog.objects.get(pk=caught_up['queryId']).index_incomplete is False


def test_req07_a_failed_extraction_is_not_lag(client, indexed):
    extraction(text='', outcome='failed')
    index_once()
    assert ask(client, 'Where is the unmistakable zeppelin hangar?')[1]['incomplete'] is False


# --- AC-10: drop the index, rebuild, and the same question set returns the same passages ----

def _answers(client):
    return {q: [(p['class'], p['scope'], p['text'], json.dumps(p['reference'], sort_keys=True))
                for p in passages(client, q)] for q, _ in FACT_QUESTIONS}


def test_ac10_dropping_the_index_and_rebuilding_returns_the_same_passages(client, indexed, core):
    before = _answers(client)
    assert all(before.values())
    call_command('migrate', 'index', 'zero', verbosity=0)
    call_command('migrate', 'index', verbosity=0)
    assert IndexUnit.objects.count() == 0
    assert passages(client, FACT_QUESTIONS[0][0]) == []
    call_command('rebuild_index')
    assert _answers(client) == before
