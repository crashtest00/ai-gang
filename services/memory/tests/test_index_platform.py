"""Corpus Indexing REQ-08's `platform` corpus: the Markdown under `docs/`
and `setup/` in the deployment's own Source checkout, every unit stamped
`platform`, re-read on a rebuild; and build brief I-23: each unit's
reference carries the checkout's commit, so a `git pull` does not orphan
its passages."""

import os
import subprocess
from datetime import datetime, timezone as dt_timezone

import pytest
from django.core.management import call_command

from index.models import IndexFailure
from index.status import index_status
from tests.index_support import (  # noqa: F401 - fixtures
    archive_api, checkout, commit_all, core, git, index_once, redis_client, search, units,
)

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def ready(core, redis_client, archive_api, checkout):
    return checkout


def _show(root, commit, path):
    return subprocess.run(['git', '-C', str(root), 'show', f'{commit}:{path}'], check=True, capture_output=True,
                          text=True).stdout


def test_req08_only_tracked_markdown_under_docs_and_setup_is_platform_corpus(ready):
    index_once()
    platform = units(source_class='platform_document')
    assert {u.reference['path'] for u in platform} == {'docs/UserGuide.md', 'setup/graphs/engine/README.md'}
    assert {u.scope for u in platform} == {'platform'}
    head = git(ready, 'rev-parse', 'HEAD')
    assert {u.reference['commit'] for u in platform} == {head}
    assert search('leftpad') == [] and search('outside docs') == []


def test_i23_a_pull_does_not_orphan_a_platform_passage_and_the_next_start_follows_it(ready):
    index_once()
    old = search('aurora valve')[0]
    (ready / 'docs' / 'UserGuide.md').write_text('# User guide\n\nThe borealis valve replaced the old one.\n')
    new_commit = commit_all(ready)
    # Before the indexer follows, the old passage still resolves at its commit.
    assert _show(ready, old.reference['commit'], 'docs/UserGuide.md')[old.reference['offset']:old.reference['end']] \
        == old.text
    index_once()  # a restart reads the checkout's new commit
    assert search('aurora valve') == []
    fresh = search('borealis valve')[0]
    assert fresh.reference['commit'] == new_commit
    assert _show(ready, new_commit, fresh.reference['path'])[fresh.reference['offset']:fresh.reference['end']] \
        == fresh.text


def _committed_at(root, *args):
    return datetime.fromisoformat(git(root, 'log', '-1', '--format=%cI', *args))


def test_kr_req05_each_platform_unit_is_dated_by_the_last_commit_to_touch_its_document(ready):
    # The engine README is left at the first commit; the user guide changes in a later one.
    first = git(ready, 'rev-parse', 'HEAD')
    (ready / 'docs' / 'UserGuide.md').write_text('# User guide\n\nThe aurora valve is reset twice.\n')
    subprocess.run(['git', '-C', str(ready), '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qam', 'two'],
                   check=True, env={**os.environ, 'GIT_COMMITTER_DATE': '2030-01-02T03:04:05+00:00'})
    index_once()
    by_path = {}
    for unit in units(source_class='platform_document'):
        assert unit.written_at is not None, unit.reference['path']
        by_path.setdefault(unit.reference['path'], set()).add(unit.written_at)
    assert by_path['docs/UserGuide.md'] == {datetime(2030, 1, 2, 3, 4, 5, tzinfo=dt_timezone.utc)}
    assert by_path['setup/graphs/engine/README.md'] == {_committed_at(ready, first)}


def test_req08_an_uncommitted_edit_in_the_checkout_is_not_indexed(ready):
    (ready / 'docs' / 'UserGuide.md').write_text('# Draft\n\nThe tundra valve is unreleased.\n')
    index_once()
    assert search('tundra valve') == [] and search('aurora valve')


def test_req03_a_rebuild_rereads_the_platform_corpus(ready):
    index_once()
    call_command('rebuild_index')
    assert search('aurora valve') and {u.scope for u in units(source_class='platform_document')} == {'platform'}


def test_req02_a_checkout_with_no_commit_is_a_recorded_failure_not_unreferenced_units(ready, settings, tmp_path):
    bare = tmp_path / 'not-a-checkout'
    (bare / 'docs').mkdir(parents=True)
    (bare / 'docs' / 'Guide.md').write_text('# Guide\n\nThe quasar valve.\n')
    settings.MEMORY_PLATFORM_CORPUS_ROOT = str(bare)
    index_once()
    assert units(source_class='platform_document') == []
    failure = IndexFailure.objects.get(source_key=f'platform:{bare}')
    assert failure.reason
    assert index_status()['incomplete']


def test_req08_a_directory_inside_another_repository_is_not_taken_for_the_checkout(ready, settings):
    inner = ready / 'docs'
    settings.MEMORY_PLATFORM_CORPUS_ROOT = str(inner)
    index_once()
    assert units(source_class='platform_document') == []
    assert 'not the top of a git checkout' in IndexFailure.objects.get(source_key=f'platform:{inner}').reason


def test_req09_a_platform_corpus_that_fails_to_index_serves_no_earlier_passage_until_a_retry_succeeds(
        ready, settings, tmp_path):
    """Row 33: the platform corpus's failure is keyed by its checkout and its
    units by document; a failure removes them all, and the retry restores
    them at the new commit."""
    index_once()
    assert search('aurora valve')
    model_dir = settings.MEMORY_EMBED_MODEL_DIR
    (ready / 'docs' / 'UserGuide.md').write_text('# User guide\n\nThe borealis valve replaced the old one.\n')
    new_commit = commit_all(ready)
    settings.MEMORY_EMBED_MODEL_DIR = str(tmp_path / 'no-model')  # the model cannot run
    index_once()
    failure = IndexFailure.objects.get(source_key=f'platform:{ready}')
    assert 'ModelUnavailable' in failure.reason
    assert units(source_class='platform_document') == [] and search('aurora valve') == []
    assert index_status()['incomplete']

    settings.MEMORY_EMBED_MODEL_DIR = model_dir
    call_command('index_retry')
    assert not IndexFailure.objects.exists()
    assert search('aurora valve') == [] and search('borealis valve')
    assert {u.reference['commit'] for u in units(source_class='platform_document')} == {new_commit}


def test_req09_a_checkout_that_cannot_be_read_removes_the_platform_passages_it_had(ready, settings):
    index_once()
    assert search('aurora valve')
    inner = ready / 'docs'
    settings.MEMORY_PLATFORM_CORPUS_ROOT = str(inner)
    index_once()
    assert IndexFailure.objects.filter(source_key=f'platform:{inner}').exists()
    assert units(source_class='platform_document') == []
