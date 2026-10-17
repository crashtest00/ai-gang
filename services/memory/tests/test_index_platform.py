"""Corpus Indexing REQ-08's `platform` corpus: the Markdown under `docs/`
and `setup/` in the deployment's own Source checkout, every unit stamped
`platform`, re-read on a rebuild; and build brief I-23: each unit's
reference carries the checkout's commit, so a `git pull` does not orphan
its passages."""

import subprocess

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
