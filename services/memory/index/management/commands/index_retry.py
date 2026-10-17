"""Retry recorded indexing failures now, without a rebuild (Corpus
Indexing REQ-09). memory-indexer also retries them on its own every
MEMORY_INDEX_RETRY_SECONDS."""

from django.core.management.base import BaseCommand

from index import follow, store


class Command(BaseCommand):
    help = 'Retry recorded indexing failures (REQ-09).'

    def add_arguments(self, parser):
        parser.add_argument('--source', default=None, help='Retry only this source key.')

    def handle(self, *args, source=None, **options):
        with store.index_lock():
            fixed = follow.retry_failures(source)
        self.stdout.write(f'{fixed} record(s) indexed on retry')
