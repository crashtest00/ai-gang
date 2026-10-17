"""Drop the whole index and rebuild it from the authoritative records
alone (Corpus Indexing REQ-03): every class from its owner's read, from no
cursor; library files from the retained extraction records; the platform
corpus from the checkout. Safe to run beside memory-indexer: both take the
index's advisory lock."""

import json

from django.core.management.base import BaseCommand

from index import follow, store


class Command(BaseCommand):
    help = 'Drop and rebuild the corpus index from source (REQ-03).'

    def handle(self, *args, **options):
        with store.index_lock():
            counts = follow.rebuild()
        self.stdout.write(json.dumps(counts, sort_keys=True))
