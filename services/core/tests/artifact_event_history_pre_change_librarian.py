"""
A pre-change librarian process — the subscriber ``manage.py run_librarian``
ran at ``6077676``, which published each answer straight to Redis before
acknowledging the request. Its handler is the frozen pre-change
``handle_request`` in ``tests/artifact_event_history_pre_change.py``;
everything else is the real ``workitems.streams`` consumer, configured as
the command configures it.

Run as a script by ``test_artifact_event_history_switch.py`` with the
consumer name as its only argument, so the test can kill it mid-request;
not collected by pytest (``pytest.ini``'s ``python_files = test_*.py``).
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

CORE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CORE_ROOT))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')

import django  # noqa: E402

django.setup()

from librarian.envelope import REQUEST_KIND  # noqa: E402
from librarian.stream_topology import REQUEST_GROUP, REQUEST_STREAM  # noqa: E402
from workitems.redis_client import new_client  # noqa: E402
from workitems.streams import PermanentError, create_consumer  # noqa: E402

from tests.artifact_event_history_pre_change import handle_request  # noqa: E402

publish_client = new_client()


def _handler(entry_envelope):
    kind = entry_envelope.get('kind')
    if kind != REQUEST_KIND:
        raise PermanentError(f'not a request: kind={kind!r}')
    handle_request(publish_client, entry_envelope)


consumer = create_consumer(new_client, stream=REQUEST_STREAM, group=REQUEST_GROUP, consumer_name=sys.argv[1],
                           handler=_handler)
consumer.batch_size = 1
consumer.start()
while True:
    time.sleep(0.5)
