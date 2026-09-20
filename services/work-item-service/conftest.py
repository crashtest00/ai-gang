"""
Shared pytest configuration. Sets environment variables BEFORE Django's
settings module is ever imported (this file is collected first, ahead of
pytest-django's own django.setup() call in its pytest_configure hook) so
workitemservice/settings.py picks up the real test Postgres/Redis
containers (services/work-item-service/docker-compose.test.yml) and the SAME
fixture agent catalog scrummaster's own tests use — mirrors the Node
implementation's test/helpers/testDb.js exactly, including why: catalog-
backed assignment validation (workitems/assignment.py, reused here too)
needs a real catalog to validate against.
"""

import importlib.util
import os
from pathlib import Path

os.environ.setdefault('PGHOST', 'localhost')
os.environ.setdefault('PGPORT', '15432')
os.environ.setdefault('PGUSER', 'workitem')
os.environ.setdefault('PGPASSWORD', 'workitem')
os.environ.setdefault('PGDATABASE', 'workitem')

_REPO_ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault('AGENTS_CATALOG_PATH', str(_REPO_ROOT / 'scrummaster' / 'test' / 'fixtures' / 'agents.json'))
os.environ.setdefault('PROJECTS_CONFIG_PATH', str(_REPO_ROOT / 'scrummaster' / 'test' / 'fixtures' / 'projects.json'))

os.environ.setdefault('REDIS_TEST_URL', 'redis://localhost:16399')
os.environ.setdefault('REDIS_URL', os.environ['REDIS_TEST_URL'])

import pytest  # noqa: E402
import redis as redis_lib  # noqa: E402

# Force-discovery precondition checks. No README documents these setup
# steps anywhere in the repo, and a new one is easy for another agent to
# never read — so instead the test run itself is the discovery mechanism:
# whoever runs pytest here hits a specific, actionable error immediately,
# rather than the confusing failures these two gaps actually produce
# (a bare `ModuleNotFoundError: whitenoise` thrown from inside Django's
# middleware-loading machinery, or a `ValueError: Missing staticfiles
# manifest entry` thrown from deep inside staticfiles storage lookup —
# neither says what to run to fix it). Checked unconditionally, not only
# for tests that happen to exercise Django's admin/HTTP surface, so the
# gap can't stay latent just because today's test selection didn't trip it.
if importlib.util.find_spec('whitenoise') is None:
    raise pytest.UsageError(
        "whitenoise is not installed in this venv. workitemservice/settings.py's "
        "WhiteNoiseMiddleware (added for the Django admin's static files, "
        "requirements.txt) needs it. Fix:\n"
        "    pip install -r services/work-item-service/requirements.txt"
    )

_STATIC_MANIFEST = Path(__file__).resolve().parent / 'staticfiles' / 'staticfiles.json'
if not _STATIC_MANIFEST.exists():
    raise pytest.UsageError(
        f"Static files haven't been collected ({_STATIC_MANIFEST} is missing). "
        "WhiteNoise's CompressedManifestStaticFilesStorage (settings.py STORAGES) "
        "requires this manifest before any Django admin page will render. Fix:\n"
        "    cd services/work-item-service && python manage.py collectstatic --noinput"
    )


@pytest.fixture
def redis_client():
    """A real redis-py client against the test Redis container — flushed
    before each test so Streams-based tests never see cross-test state."""
    client = redis_lib.Redis.from_url(os.environ['REDIS_TEST_URL'], decode_responses=True)
    client.flushdb()
    yield client
    client.close()


@pytest.fixture
def redis_factory():
    """A callable that builds fresh redis-py clients — the same shape
    workitems.streams.Consumer/create_consumer expect (a factory, not a
    single shared client, mirroring streams.js's `client.duplicate()` for
    the blocking read connection)."""
    def factory():
        return redis_lib.Redis.from_url(os.environ['REDIS_TEST_URL'], decode_responses=True)
    return factory


# Every table this app owns, in an order TRUNCATE ... CASCADE handles
# regardless of FK direction (CASCADE makes explicit ordering unnecessary,
# but listed in the same order test/helpers/testDb.js used, for easy
# comparison against the reference implementation).
_TABLES = [
    'work_item_comment', 'work_item_artifact', 'work_item_history', 'work_item_link',
    'work_item_story_detail', 'work_item_release_detail', 'outbox_event', 'webhook_failure', 'access_log',
    'work_item', 'project_status_config', 'project_config',
]


@pytest.fixture
def clean_db(transactional_db):
    """Truncates every table this app owns before the test body runs.

    Deliberately uses pytest-django's `transactional_db` (TransactionTestCase
    style — every write is a REAL commit, nothing wrapped in a rolled-back
    outer transaction) rather than the default `db` fixture. This is not
    optional: several of our tests exercise a background thread (a Streams
    Consumer) or a genuinely separate OS process (the relay subprocess in
    tests/test_relay_integration.py) that opens its OWN database connection
    — Django's per-test rollback wrapping only ever covers the connection
    on the main test thread, so a background thread/process's writes would
    not be visible to (or undone by) it. Explicit truncation between tests,
    matching the Node reference implementation's test/helpers/testDb.js
    truncateAll() exactly, is what actually gives every test a clean slate
    here."""
    from django.db import connection
    with connection.cursor() as cursor:
        cursor.execute(f"TRUNCATE {', '.join(_TABLES)} CASCADE")
    yield
