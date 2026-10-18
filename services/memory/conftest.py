"""Memory's test configuration.

The suite runs on the host against the shared test PostgreSQL container
(run_tests.sh exports the connection); each test gets its own temporary
harness volume and archive under pytest's tmp_path, and a clock it moves by
hand so REQ-06's 35-minute interval is exercised without waiting for it.
"""

import os
from datetime import datetime, timedelta, timezone as dt_timezone

os.environ.setdefault('PGHOST', 'localhost')
os.environ.setdefault('PGPORT', '15432')
os.environ.setdefault('PGUSER', 'workitem')
os.environ.setdefault('PGPASSWORD', 'workitem')
os.environ.setdefault('PGDATABASE', 'memory')

import pytest  # noqa: E402


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 12, 12, 0, tzinfo=dt_timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, **kwargs):
        self.now += timedelta(**kwargs)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def roots(tmp_path, settings):
    harness = tmp_path / 'harness'
    archive = tmp_path / 'archive'
    harness.mkdir()
    archive.mkdir()
    settings.MEMORY_HARNESS_ROOT = str(harness)
    settings.MEMORY_ARCHIVE_ROOT = str(archive)
    return harness, archive


@pytest.fixture
def copier(roots, clock):
    from archive.copier import Copier
    harness, archive = roots
    return Copier(harness_root=harness, archive_root=archive, clock=clock)
