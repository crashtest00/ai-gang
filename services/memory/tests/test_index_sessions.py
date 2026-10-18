"""Corpus Indexing REQ-10 (a session is indexed turn by turn, subagents
linked to their starting tool call, oversized tool results as stubs, no
bookkeeping record indexed), REQ-04's session rule (a run's steps are
indexed once its end record is archived or the session goes quiet, never
while it runs) and REQ-01's `memory/` notes, through the real archive:
the copier fills it, memory-api serves it, the indexer reads it."""

import json

import pytest

from index.models import IndexFailure
from index.status import index_status
from tests.index_support import (  # noqa: F401 - fixtures
    archive_api, asst, chained, checkout, core, index_once, prompt, redis_client, resolve_session_unit, search,
    text_block, thinking, tool_use, units,
)
from tests.support import Session as Src, conv, cost_state, enqueue, line

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def indexer(core, redis_client, archive_api, checkout):
    """Everything the indexer reads, with no Core or library records."""
    return archive_api


def _result(tool_use_id, text):
    record = conv('user', content=[{'type': 'tool_result', 'tool_use_id': tool_use_id, 'content': text}])
    return record


def _line_of(path, needle):
    for number, raw in enumerate(path.read_bytes().split(b'\n'), 1):
        if needle.encode() in raw:
            return number
    raise AssertionError(needle)


def test_req10_turns_subagents_stubs_and_no_bookkeeping(indexer, roots, copier):
    harness, _ = roots
    s = Src(harness)
    big = 'LOGLINE ' * 700  # 5,600 characters: above the stub threshold
    s.append([enqueue('ZEBRAPROMPT do the work'), {'type': 'ai-title', 'title': 'zebratitle', 'sessionId': 's'}]
             + chained(prompt('Fix the capybara build.'),
                       asst('m1', thinking('I should delegate the search.')),
                       asst('m1', tool_use('toolu_A', 'Agent', {'prompt': 'find the marmot'})),
                       _result('toolu_A', 'The subagent found the marmot in config.'),
                       asst('m2', tool_use('toolu_B', 'Bash', {'command': 'cat build.log'})),
                       _result('toolu_B', big),
                       asst('m3', text_block('The capybara build is fixed.')))
             + [{'type': 'yak-unknown-type', 'note': 'yakunknown'}, b'kiwiunparse not json\n', cost_state()])
    sub = s.sidecar('subagents', 'agent-a1.jsonl')
    s.append(chained(prompt('find the marmot'), asst('s1', text_block('Found the marmot under config/.'))), path=sub)
    s.write('subagents/agent-a1.meta.json', json.dumps({'agentType': 'general-purpose',
                                                         'toolUseId': 'toolu_A'}).encode())
    copier.reconcile()
    index_once()

    main_units = [u for u in units(source_class='session') if u.reference['file'].endswith(f'{s.id}.jsonl')]
    assert {u.reference['turn'] for u in main_units} == {'m1', 'm2', 'm3'}
    for unit in main_units:
        resolved, _ = resolve_session_unit(indexer, unit)
        assert resolved == unit.text
    m1 = next(u for u in main_units if u.reference['turn'] == 'm1')
    # One turn: the prompt it answers, the reasoning, the tool call and its result.
    assert 'Fix the capybara build.' in m1.text and 'delegate the search' in m1.text
    assert 'Tool call Agent (toolu_A)' in m1.text and 'found the marmot in config' in m1.text
    assert 'capybara build is fixed' not in m1.text

    m2 = next(u for u in main_units if u.reference['turn'] == 'm2')
    result_line = _line_of(s.transcript, 'LOGLINE')
    assert f'Tool result Bash (toolu_B): [stub: {len(big)} characters at archive line {result_line}' in m2.text
    assert search('LOGLINE') == []

    sub_units = [u for u in units(source_class='session') if u.reference['file'].endswith('agent-a1.jsonl')]
    assert sub_units and all(u.reference['parentSessionId'] == s.id and u.reference['toolUseId'] == 'toolu_A'
                             for u in sub_units)
    assert any('Found the marmot' in u.text for u in sub_units)
    for unit in sub_units:
        assert resolve_session_unit(indexer, unit)[0] == unit.text

    everything = ' '.join(u.text for u in units())
    for bookkeeping in ('ZEBRAPROMPT', 'zebratitle', 'yakunknown', 'kiwiunparse'):
        assert bookkeeping not in everything


def test_req10_a_turn_follows_the_parentuuid_chain_not_file_order(indexer, roots, copier):
    harness, _ = roots
    s = Src(harness)
    p = prompt('Choose a name for the otter service.')
    a1 = asst('m1', thinking('Branch one: call it otterd.'))
    a2 = asst('m1', text_block('Answer one: otterd.'))
    b1 = asst('m2', thinking('Branch two: call it lutra.'))
    b2 = asst('m2', text_block('Answer two: lutra.'))
    chained(p, a1, a2)
    b1['parentUuid'], b2['parentUuid'] = p['uuid'], b1['uuid']
    s.append([enqueue(), p, a1, b1, a2, b2, cost_state()])  # the branches interleave in the file
    copier.reconcile()
    index_once()
    by_turn = {u.reference['turn']: u for u in units(source_class='session')}
    assert set(by_turn) == {'m1', 'm2'}
    assert 'otterd' in by_turn['m1'].text and 'lutra' not in by_turn['m1'].text
    assert 'lutra' in by_turn['m2'].text and 'otterd' not in by_turn['m2'].text
    assert by_turn['m1'].reference['turnLines'] == [2, 3, 5]
    assert by_turn['m2'].reference['turnLines'] == [2, 4, 6]


def test_req10_an_oversized_turn_is_split_into_parts_that_each_resolve(indexer, roots, copier):
    harness, _ = roots
    s = Src(harness)
    paragraphs = '\n\n'.join(f'Paragraph {i} about the tapir rollout. ' + 'detail ' * 120 for i in range(12))
    s.append([enqueue()] + chained(prompt('Write the tapir plan.'), asst('m1', text_block(paragraphs)))
             + [cost_state()])
    copier.reconcile()
    index_once()
    parts = units(source_class='session')
    assert len(parts) > 1 and {u.reference['turn'] for u in parts} == {'m1'}
    assert all(len(u.text) <= 4000 for u in parts)
    assert [u.reference['offset'] for u in parts] == sorted(u.reference['offset'] for u in parts)
    for unit in parts:
        assert resolve_session_unit(indexer, unit)[0] == unit.text


def test_req04_a_runs_steps_are_indexed_once_its_end_record_is_archived_not_while_it_runs(indexer, roots, copier):
    harness, _ = roots
    s = Src(harness)
    s.append([enqueue()] + chained(prompt('First run.'), asst('m1', text_block('The gecko check passed.')))
             + [cost_state()])
    s.append([enqueue()] + chained(prompt('Second run.'), asst('m2', text_block('Now checking the iguana.'))))
    copier.reconcile()
    index_once()
    assert search('gecko') and search('iguana') == []

    s.append([cost_state()])
    copier.reconcile()
    index_once()
    assert search('iguana')


def test_req04_a_killed_run_is_indexed_when_the_session_goes_quiet(indexer, roots, copier, clock):
    harness, _ = roots
    s = Src(harness)
    s.append([enqueue()] + chained(prompt('Run.'), asst('m1', text_block('The pangolin job was killed.'))))
    copier.reconcile()
    index_once()
    assert search('pangolin') == []
    clock.advance(minutes=36)
    copier.sweep_quiet()
    index_once()
    assert search('pangolin')


def test_req10_a_subagent_whose_meta_names_no_tool_call_is_a_recorded_failure(indexer, roots, copier):
    harness, _ = roots
    s = Src(harness)
    s.append([enqueue()] + chained(prompt('Go.'), asst('m1', text_block('Delegating.'))) + [cost_state()])
    sub = s.sidecar('subagents', 'agent-b2.jsonl')
    s.append(chained(prompt('sub'), asst('s1', text_block('The dugong was found.'))), path=sub)
    s.write('subagents/agent-b2.meta.json', b'{"agentType": "general-purpose"}')
    copier.reconcile()
    index_once()
    failure = IndexFailure.objects.get()
    assert failure.source_key.endswith('agent-b2.jsonl') and 'toolUseId' in failure.reason
    assert search('dugong') == []
    assert index_status()['incomplete']


def test_req01_a_notes_units_are_added_replaced_and_removed_with_the_note(indexer, roots, copier):
    harness, _ = roots
    s = Src(harness)
    s.append([enqueue()])
    note = s.notes_dir() / 'deploys.md'
    note.write_bytes(b'# Deploys\n\nThe heron deploy needs approval.\n')
    copier.reconcile()
    index_once()
    first = search('heron')
    assert first and first[0].reference['path'].endswith('memory/deploys.md')
    first_digest = first[0].reference['digest']

    note.write_bytes(b'# Deploys\n\nThe egret deploy runs itself.\n')
    copier.reconcile()
    index_once()
    assert search('heron') == []
    replaced = search('egret')
    assert replaced and replaced[0].reference['digest'] != first_digest

    note.unlink()
    copier.reconcile()
    index_once()
    assert search('egret') == []


def test_bookkeeping_lines_alone_produce_no_units(indexer, roots, copier):
    harness, _ = roots
    s = Src(harness)
    s.append([enqueue('only bookkeeping'), cost_state(), line({'type': 'mode', 'mode': 'auto'})])
    copier.reconcile()
    index_once()
    assert units(source_class='session') == []


def test_req04_a_growing_session_is_reindexed_without_embedding_its_unchanged_turns_again(
        indexer, roots, copier, monkeypatch):
    """Row 31: an active session's archive copy grows with every record its
    running turn writes. Each poll re-reads it, but the turns already
    indexed are not embedded again: while the run is going nothing is
    written, and when it ends only the new turn is embedded."""
    from index import embedding
    harness, _ = roots
    s = Src(harness)
    s.append([enqueue()] + chained(prompt('First run.'), asst('m1', text_block('The gecko check passed.')))
             + [cost_state()])
    running = chained(prompt('Second run.'), asst('m2', text_block('Now checking the iguana.')))
    s.append([enqueue()] + running)
    copier.reconcile()
    index_once()
    key = next(u.source_key for u in search('gecko'))
    before = {u.text: (bytes(u.embedding), u.indexed_at) for u in units(source_key=key)}
    assert before

    embedded = []
    real = embedding.embed_units
    monkeypatch.setattr(embedding, 'embed_units', lambda texts: embedded.extend(texts) or real(texts))

    # The run goes on: its transcript grows, its last end record does not move.
    s.append(chained(asst('m3', text_block('Still checking the iguana.')), parent=running[-1]['uuid']))
    copier.reconcile()
    index_once()
    assert embedded == []
    assert {u.text: (bytes(u.embedding), u.indexed_at) for u in units(source_key=key)} == before

    # The run ends: its turn is embedded, the first run's turn is not again.
    s.append([cost_state()])
    copier.reconcile()
    index_once()
    after = units(source_key=key)
    new = [u.text for u in after if u.text not in before]
    assert new and search('iguana')
    assert sorted(embedded) == sorted(set(new))
    for unit in after:
        if unit.text in before:
            assert bytes(unit.embedding) == before[unit.text][0]
