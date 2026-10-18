"""Track 5: every unit carries its embedding from the local model, kept as
derived and rebuildable as the rest of the index (Corpus Indexing REQ-03),
and computed inside the deployment from weights that are never fetched
(REQ-06; build brief I-08).

The model is read from the directory run_tests.sh names
(MEMORY_EMBED_MODEL_DIR), filled once by scripts/fetch-embedding-model.sh;
no test here downloads anything."""

import dataclasses
import re
import socket
from pathlib import Path

import numpy as np
import pytest
from django.core.management import call_command
from django.db import IntegrityError, connection

from index import embedding, store
from index.models import CLASS_LIBRARY, IndexFailure, IndexUnit
from index.status import index_status
from tests.index_support import (  # noqa: F401 - fixtures
    archive_api, checkout, core, index_once, redis_client, search, seeded, units,
)

pytestmark = pytest.mark.django_db(transaction=True)


def _library(key, text):
    store.replace_units(key, CLASS_LIBRARY, {'eval': key}, [{
        'text': text,
        'reference': {'artifactId': '6c2c3f4e-0f2e-4b9a-9d41-2d6d6c3a7e10',
                      'stamp': {'updatedAt': '2026-10-01T00:00:00+00:00', 'sha256': 'e' * 64},
                      'lineStart': 1, 'lineEnd': 1, 'offset': 0, 'end': len(text)}}])
    return IndexUnit.objects.get(source_key=key)


def test_every_unit_written_carries_its_texts_embedding():
    unit = _library('artifact:a', 'The osprey nest is inspected every spring.')
    vectors = embedding.unpack(unit.embedding)
    assert vectors.shape == (1, embedding.MODEL.dim)
    assert np.isclose(np.linalg.norm(vectors[0]), 1.0, atol=1e-4)
    fresh = embedding.unpack(embedding.embed_units([unit.text])[0])
    assert np.allclose(vectors, fresh, atol=1e-4)


def test_the_database_refuses_a_unit_without_an_embedding_of_whole_vectors():
    unit = _library('artifact:a', 'The osprey nest is inspected every spring.')
    for bad in (b'', b'\x00' * (embedding.MODEL.dim * 4 - 4)):
        with pytest.raises(IntegrityError, match='index_unit_embedding_whole_vectors'):
            with connection.cursor() as cur:
                cur.execute('UPDATE index_unit SET embedding = %s WHERE id = %s', [bad, unit.id])


def test_a_unit_whose_text_changes_is_embedded_again():
    before = _library('artifact:a', 'The osprey nest is inspected every spring.')
    after = _library('artifact:a', 'Invoices are exported as CSV on the first of the month.')
    assert not np.allclose(embedding.unpack(before.embedding), embedding.unpack(after.embedding), atol=1e-3)
    fresh = embedding.unpack(embedding.embed_units([after.text])[0])
    assert np.allclose(embedding.unpack(after.embedding), fresh, atol=1e-4)


def test_a_text_longer_than_the_models_input_is_embedded_whole_as_overlapping_windows(monkeypatch):
    monkeypatch.setattr(embedding, 'MODEL', dataclasses.replace(embedding.MODEL, max_tokens=100))
    model = embedding.model()
    text = ' '.join(f'word{n}' for n in range(200))
    ids = model.tokenizer.encode(text, add_special_tokens=False).ids
    windows = model.windows(text)
    assert len(windows) > 1 and all(len(w) <= 100 for w in windows)
    assert all(w[0] == model.cls and w[-1] == model.sep for w in windows)
    # No token is cut off: the windows cover the text, the last one ends it.
    covered = [t for w in windows for t in w[1:-1]]
    assert set(covered) == set(ids) and windows[-1][-2] == ids[-1]
    blob = embedding.embed_units([text])[0]
    assert embedding.unpack(blob).shape == (len(windows), embedding.MODEL.dim)


def test_rebuild_embeds_every_unit_afresh(seeded, core, roots, monkeypatch):
    index_once()
    before = {(u.source_key, u.position): embedding.unpack(u.embedding) for u in IndexUnit.objects.all()}
    embedded = []
    real = embedding.embed_units
    monkeypatch.setattr(embedding, 'embed_units', lambda texts: embedded.extend(texts) or real(texts))
    call_command('rebuild_index')
    units = list(IndexUnit.objects.all())
    assert len(embedded) == len(units) == len(before)
    for unit in units:
        assert np.allclose(embedding.unpack(unit.embedding), before[(unit.source_key, unit.position)], atol=1e-4)


def test_a_missing_model_is_an_error_recorded_as_the_records_failure_never_a_download(
        core, redis_client, archive_api, checkout, settings, tmp_path, monkeypatch):
    model_dir = settings.MEMORY_EMBED_MODEL_DIR
    settings.MEMORY_EMBED_MODEL_DIR = str(tmp_path / 'no-model')
    connections = []
    monkeypatch.setattr(socket.socket, 'connect', lambda self, address: connections.append(address) or
                        (_ for _ in ()).throw(AssertionError(f'network connection to {address}')))
    with pytest.raises(embedding.ModelUnavailable, match='never downloads'):
        embedding.model()
    assert connections == []
    monkeypatch.undo()
    settings.MEMORY_EMBED_MODEL_DIR = str(tmp_path / 'no-model')

    item = core.work_item('Check the heron pond')
    index_once()
    failure = IndexFailure.objects.get(source_key=f"work_item:{item['id']}")
    assert 'ModelUnavailable' in failure.reason
    assert search('heron pond') == []
    assert index_status()['incomplete']

    # REQ-09: once the model is back, the indexer's own retry indexes it.
    settings.MEMORY_EMBED_MODEL_DIR = model_dir
    index_once()
    assert not IndexFailure.objects.exists()
    [unit] = search('heron pond')
    assert embedding.unpack(unit.embedding).shape == (1, embedding.MODEL.dim)


def test_loading_the_model_opens_no_network_connection(monkeypatch):
    embedding._loaded.clear()
    connections = []

    def refuse(self, address):
        connections.append(address)
        raise AssertionError(f'network connection to {address}')

    monkeypatch.setattr(socket.socket, 'connect', refuse)
    vectors = embedding.embed_units(['The kingfisher perch was repainted.'])
    query = embedding.embed_query('Who repainted the perch?')
    assert connections == [] and vectors and query.shape == (embedding.MODEL.dim,)


# --- Row 31: an unchanged text keeps its vector (REQ-04's freshness bound) ------

@pytest.fixture
def embedded(monkeypatch):
    """Every text the real model is asked to embed, from here on."""
    texts = []
    real = embedding.embed_units
    monkeypatch.setattr(embedding, 'embed_units', lambda batch: texts.extend(batch) or real(batch))
    return texts


def _vectors(source_key):
    return {u.text: bytes(u.embedding) for u in units(source_key=source_key)}


def test_req04_a_work_item_that_gains_history_and_a_comment_embeds_only_the_new_text(
        core, redis_client, archive_api, checkout, embedded):
    item = core.work_item('Calibrate the narwhal sensor', 'The narwhal sensor drifts in cold rooms.',
                          history=[('status', 'todo', 'in_progress')])
    index_once()
    key = f"work_item:{item['id']}"
    before = _vectors(key)
    assert len(before) == 2  # its fields and its history

    embedded.clear()
    item['history'].append({'id': 'h-2', 'work_item_id': item['id'], 'field': 'status', 'old_value': 'in_progress',
                            'new_value': 'review', 'actor': 'agent', 'occurred_at': '2026-10-18T10:00:00+00:00'})
    core.put('work_items', item)
    comment = core.comment(item['id'], 'The pelican cache was the culprit.')
    index_once()

    after = _vectors(key)
    kept = {text for text in after if text in before}
    changed = set(after) - kept
    assert len(kept) == 1 and len(changed) == 1  # the fields unchanged, the history grown
    assert all(after[text] == before[text] for text in kept)
    assert set(embedded) == changed | set(_vectors(f"comment:{comment['id']}"))
    assert len(embedded) == len(set(embedded))


def test_req04_an_a2a_task_that_gains_a_message_embeds_only_the_new_message(
        core, redis_client, archive_api, checkout, embedded):
    task = core.a2a_task(['Requesting review of the ocelot migration.'])
    index_once()
    key = f"a2a:{task['id']}"
    before = _vectors(key)
    embedded.clear()
    task['messages'].append({**task['messages'][0], 'messageId': 'msg-second', 'entryId': '1-0',
                             'parts': [{'kind': 'text', 'text': 'The ocelot migration is approved.'}]})
    core.put('a2a', task)
    index_once()
    after = _vectors(key)
    assert set(before) < set(after)
    assert all(after[text] == vector for text, vector in before.items())
    assert set(embedded) == set(after) - set(before)


def test_req03_an_edited_record_is_embedded_again_through_the_indexer(core, redis_client, archive_api, checkout):
    """Row 34: a record edited at its owner gets its new text's vector when
    the indexer follows it, not the old text's."""
    item = core.work_item('Check the heron pond', 'The heron pond freezes in January.')
    index_once()
    [old] = search('heron pond freezes')
    item['description'] = 'Invoices are exported as CSV on the first of the month.'
    core.put('work_items', item)
    index_once()
    assert search('heron pond freezes') == []
    [new] = search('invoices exported')
    assert new.text != old.text
    assert not np.allclose(embedding.unpack(old.embedding), embedding.unpack(new.embedding), atol=1e-3)
    fresh = embedding.unpack(embedding.embed_units([new.text])[0])
    assert np.allclose(embedding.unpack(new.embedding), fresh, atol=1e-4)


# --- Row 35: the model directory must hold MODEL, by its marker -------------------

def test_the_fetch_scripts_pin_is_the_model_embedding_py_embeds_with():
    script = (Path(__file__).resolve().parents[1] / 'scripts' / 'fetch-embedding-model.sh').read_text()
    repo = re.search(r"^repo='([^']+)'$", script, re.M).group(1)
    revision = re.search(r"^revision='([^']+)'$", script, re.M).group(1)
    assert (repo, revision) == (embedding.MODEL.name, embedding.MODEL.revision)
    # And the marker it writes is the one the model directory is checked against.
    assert 'echo "$repo@$revision" > "$dest/MODEL"' in script
    assert embedding.pinned() == f'{repo}@{revision}'


def _model_dir_with_marker(tmp_path, marker):
    """The real model's files, under a directory whose marker is ``marker``
    (None: no marker)."""
    real = embedding.settings.MEMORY_EMBED_MODEL_DIR
    directory = tmp_path / 'model'
    directory.mkdir()
    for name in embedding.MODEL_FILES:
        (directory / name).symlink_to(Path(real) / name)
    if marker is not None:
        (directory / embedding.MARKER).write_text(marker + '\n')
    return str(directory)


def test_a_model_directory_without_its_marker_is_unavailable(settings, tmp_path):
    settings.MEMORY_EMBED_MODEL_DIR = _model_dir_with_marker(tmp_path, None)
    with pytest.raises(embedding.ModelUnavailable, match='missing MODEL'):
        embedding.model()


def test_a_model_directory_whose_marker_names_another_model_is_unavailable_and_recorded_as_a_failure(
        core, redis_client, archive_api, checkout, settings, tmp_path):
    model_dir = settings.MEMORY_EMBED_MODEL_DIR
    settings.MEMORY_EMBED_MODEL_DIR = _model_dir_with_marker(
        tmp_path, f'{embedding.MODEL.name}@0000000000000000000000000000000000000000')
    with pytest.raises(embedding.ModelUnavailable, match='never downloads'):
        embedding.model()
    item = core.work_item('Check the egret marsh')
    index_once()
    failure = IndexFailure.objects.get(source_key=f"work_item:{item['id']}")
    assert 'ModelUnavailable' in failure.reason and embedding.pinned() in failure.reason
    assert search('egret marsh') == []
    settings.MEMORY_EMBED_MODEL_DIR = model_dir
    index_once()
    assert not IndexFailure.objects.exists() and search('egret marsh')
