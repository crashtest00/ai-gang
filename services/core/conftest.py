"""
Shared pytest configuration. Sets environment variables BEFORE Django's
settings module is ever imported (this file is collected first, ahead of
pytest-django's own django.setup() call in its pytest_configure hook) so
core/settings.py picks up the real test Postgres/Redis
containers (services/core/docker-compose.test.yml) and the SAME
fixture agent catalog scrummaster's own tests use — mirrors the Node
implementation's test/helpers/testDb.js exactly, including why: catalog-
backed assignment validation (workitems/assignment.py, reused here too)
needs a real catalog to validate against.
"""

import importlib.util
import os
import sys
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
        "whitenoise is not installed in this venv. core/settings.py's "
        "WhiteNoiseMiddleware (added for the Django admin's static files, "
        "requirements.txt) needs it. Fix:\n"
        "    pip install -r services/core/requirements.txt"
    )

_STATIC_MANIFEST = Path(__file__).resolve().parent / 'staticfiles' / 'staticfiles.json'
if not _STATIC_MANIFEST.exists():
    raise pytest.UsageError(
        f"Static files haven't been collected ({_STATIC_MANIFEST} is missing). "
        "WhiteNoise's CompressedManifestStaticFilesStorage (settings.py STORAGES) "
        "requires this manifest before any Django admin page will render. Fix:\n"
        "    cd services/core && python manage.py collectstatic --noinput"
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


# ---------------------------------------------------------------------------
# A wedged test database is the failure of the run that left it, not of the
# next one (V5.1 audit row 32)
# ---------------------------------------------------------------------------
#
# pytest-django's `django_db_setup` drops the test database in its finalizer,
# and when the drop fails it downgrades the exception to a warning and lets the
# session end 0:
#
#     except Exception as exc:
#         request.node.warn(pytest.PytestWarning(
#             f"Error when trying to teardown test databases: {exc!r}"))
#
# (pytest_django/fixtures.py). The drop fails when something else is connected
# to test_workitem, so the database survives the run that was supposed to
# remove it — and the *next* run is the one that finds out, at database setup,
# with nothing to say about who left it. The information lands on the victim
# instead of the cause, and run_tests.sh's preflight can only recover or refuse
# after the fact. Failing here is what stops a wedge being created silently.
#
# Why not `-W error::pytest.PytestWarning`, which this could have been in one
# line: it promotes every PytestWarning the suite can emit — an unknown mark, a
# collectable class with an __init__, a test function that returns non-None, an
# unraisable exception in a thread — so an unrelated hygiene warning anywhere in
# the suite would fail the run with this row's name on it. It also arrives in the
# wrong place: the promoted warning raises inside the session fixture's
# finalizer, after pytest-django's own `except Exception`, so it is reported as
# an error in the teardown of whichever test happened to be last, which is a
# test that has nothing to do with it. This matches on the one warning that
# means a wedge, and reports it as what it is: a property of the run, at the end
# of the run.
_TEARDOWN_FAILURE = 'Error when trying to teardown test databases'

# The guard's own exit code. Not one of pytest's 0-5, because every test may
# well have passed and the report above is valid — and not run_tests.sh's
# preflight 78 either, which means the opposite thing: 78 is "no test ran", 79
# is "every test ran and the run then left the shared test database behind".
EXIT_TEST_DATABASE_LEFT_BEHIND = 79

_TEARDOWN_GUARD = 'aigang-test-database-teardown-guard'


class DatabaseTeardownGuard:
    """Fails the session that could not drop its own test database.

    Registered as a plugin rather than written as conftest-level hook
    functions so its state is per-instance: a test can drive a fresh one
    without touching the one guarding the run it is part of.
    """

    def __init__(self):
        self.failures = []

    def pytest_warning_recorded(self, warning_message, when, nodeid, location):
        message = warning_message.message
        if isinstance(message, pytest.PytestWarning) and _TEARDOWN_FAILURE in str(message):
            self.failures.append(str(message))

    # Prints just above pytest's own warnings summary and final "N passed" line:
    # the terminal reporter implements this same hook as a wrapper, so its
    # summary is always printed outside every plain implementation of it and no
    # hook ordering here can follow it. The exit code is what makes the refusal
    # unmissable; this is what makes it legible.
    def pytest_sessionfinish(self, session, exitstatus):
        if not self.failures:
            return
        test_db = f"test_{os.environ.get('PGDATABASE', 'workitem')}"
        print(
            f'\nrun_tests.sh: this run could not drop the test database {test_db}, '
            f'because something else was connected to it:\n',
            *(f'    {failure}' for failure in self.failures),
            f"""
{test_db} is therefore still there, with those connections on it, and the next
run is the one that would have discovered that — at database setup, with no way
to tell whose run left it. Failing it here puts it on the run that caused it
(V5.1 audit row 32).

Find what is holding connections and stop it:

    docker exec -i workitem-test-postgres-django psql -U workitem -d workitem -c \\
      "SELECT pid, state, state_change, query FROM pg_stat_activity WHERE datname = '{test_db}';"

A {test_db} nobody is connected to needs nothing done to it: run_tests.sh's
preflight drops it on the next run under the suite lock, and pytest recreates
it.
""",
            sep='\n', file=sys.stderr,
        )
        if exitstatus == 0:
            # A run whose tests already failed keeps that exit code: the test
            # failures are the more important result, and the message above is
            # printed either way.
            session.exitstatus = EXIT_TEST_DATABASE_LEFT_BEHIND


def pytest_configure(config):
    config.pluginmanager.register(DatabaseTeardownGuard(), _TEARDOWN_GUARD)
