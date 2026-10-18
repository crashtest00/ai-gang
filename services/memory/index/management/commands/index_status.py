"""Print the index's lag signal (Corpus Indexing REQ-05) as JSON."""

import json

from django.core.management.base import BaseCommand

from index.status import index_status


class Command(BaseCommand):
    help = "Whether the index may be incomplete, and why (REQ-05)."

    def handle(self, *args, **options):
        self.stdout.write(json.dumps(index_status(), indent=2, sort_keys=True))
