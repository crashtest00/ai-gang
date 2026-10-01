"""
run_tests.sh's preflight gates, driven for real (V5.1 audit row 47).

`preflight.sh` is the only thing in this repository that runs
`pg_terminate_backend` followed by `DROP DATABASE`. It arrived reviewed but
untested, and the property that makes it safe is not a property of any one
line: it is that five gates must all hold before the destructive path is
reached, and that every gate which cannot be *proven* refuses instead. A review
can read five gates; only a test can show that each one still refuses.

**How this avoids touching the suite's own database.** `preflight.sh` derives
the database it inspects from `PGDATABASE` — `test_$PGDATABASE` — and takes its
suite lock from `V4_WIS_SUITE_LOCK`. Both are environment variables, so these
tests hand it a throwaway database pair of their own (`pfprobe` standing in for
the development database, `test_pfprobe` for the test one) and a lock file in a
temporary directory. Nothing here can reach `test_workitem`, the database this
very suite is running inside, or the real `/tmp/v4-wis-suite.lock`.

**How the wedge is reproduced.** The real one comes from a run killed
mid-suite: its backends stay connected to the test database, idle, with
`COMMIT` as their last statement, and Django's own drop-and-recreate cannot get
past them. `_idle_backends` reproduces exactly that — outside connections that
run `BEGIN; SELECT 1; COMMIT` and are then left alone — rather than asserting
against a double of `pg_stat_activity`.
"""

from __future__ import annotations

import fcntl
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import psycopg2
import pytest

CORE_DIR = Path(__file__).resolve().parent.parent
PREFLIGHT = CORE_DIR / 'preflight.sh'

# The stand-in pair. preflight.sh inspects 'test_' + PGDATABASE, so naming the
# development stand-in `pfprobe` is what makes `test_pfprobe` the database
# under test — and `test_workitem` unreachable from here.
PROBE_DB = 'pfprobe'
PROBE_TEST_DB = f'test_{PROBE_DB}'

# preflight.sh's own ORPHAN_IDLE_S. Not configurable there on purpose: it is
# the threshold that separates "a live suite between tests" from "an orphan",
# and a caller able to lower it could talk the script into killing a live run.
# The cost is that the one test which reaches the destructive path has to wait
# it out for real; that is the price of the gate not being overridable.
ORPHAN_IDLE_S = 10


def _conn(dbname):
    return psycopg2.connect(
        host=os.environ['PGHOST'], port=os.environ['PGPORT'],
        user=os.environ['PGUSER'], password=os.environ['PGPASSWORD'], dbname=dbname,
    )


def _admin():
    """An autocommit connection to the development database — CREATE/DROP
    DATABASE cannot run inside a transaction."""
    conn = _conn(os.environ['PGDATABASE'])
    conn.autocommit = True
    return conn


def _force_drop(cur, dbname):
    cur.execute('SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s', (dbname,))
    cur.execute(f'DROP DATABASE IF EXISTS "{dbname}"')


def _database_exists(cur, dbname):
    cur.execute('SELECT 1 FROM pg_database WHERE datname = %s', (dbname,))
    return cur.fetchone() is not None


class Probe:
    """The throwaway database pair, plus the outside connections a test leaves
    on it. Owns the reap: every connection it opens is closed in teardown,
    whether the test terminated them or not."""

    def __init__(self, admin, lock_file):
        self.admin = admin
        self.lock_file = str(lock_file)
        self._conns = []

    def _open(self, dbname=PROBE_TEST_DB):
        conn = _conn(dbname)
        conn.autocommit = True  # so the statement text below is exactly what Postgres sees
        self._conns.append(conn)
        return conn

    def idle_backends(self, count=2):
        """`count` connections to the test database whose last statement is
        COMMIT and which are then left idle — a killed run's leftovers."""
        for _ in range(count):
            cur = self._open().cursor()
            cur.execute('BEGIN')
            cur.execute('SELECT 1')
            cur.fetchone()
            cur.execute('COMMIT')
        return self.backends()

    def backend_in_transaction(self):
        """A connection that opened a transaction and has not committed —
        `idle in transaction`, which is not an orphan."""
        cur = self._open().cursor()
        cur.execute('BEGIN')
        cur.execute('SELECT 1')
        cur.fetchone()

    def backend_idle_after_a_select(self):
        """Idle, but its last statement was a SELECT, not COMMIT — work that
        may not have been committed, so it may not be an orphan."""
        cur = self._open().cursor()
        cur.execute('SELECT 1')
        cur.fetchone()

    def backends(self):
        with self.admin.cursor() as cur:
            cur.execute(
                "SELECT pid, state, coalesce(query, ''),"
                '       extract(epoch FROM (clock_timestamp() - state_change))'
                '  FROM pg_stat_activity WHERE datname = %s', (PROBE_TEST_DB,))
            # extract(epoch ...) comes back as Decimal; the idle arithmetic below
            # mixes it with floats.
            return [(pid, state, query, float(idle or 0)) for pid, state, query, idle in cur.fetchall()]

    def wedged_db_exists(self):
        with self.admin.cursor() as cur:
            return _database_exists(cur, PROBE_TEST_DB)

    def close(self):
        for conn in self._conns:
            try:
                conn.close()
            except Exception:
                pass
        self._conns.clear()


@pytest.fixture
def probe(tmp_path):
    admin = _admin()
    with admin.cursor() as cur:
        # A previous aborted run of this module could have left either behind.
        _force_drop(cur, PROBE_TEST_DB)
        _force_drop(cur, PROBE_DB)
        cur.execute(f'CREATE DATABASE "{PROBE_DB}"')
        cur.execute(f'CREATE DATABASE "{PROBE_TEST_DB}"')
    p = Probe(admin, tmp_path / 'probe-suite.lock')
    try:
        yield p
    finally:
        p.close()
        with admin.cursor() as cur:
            _force_drop(cur, PROBE_TEST_DB)
            _force_drop(cur, PROBE_DB)
        admin.close()


def run_preflight(probe, *, holding_the_lock):
    """Runs preflight.sh against the probe pair.

    `holding_the_lock=True` is the documented invocation
    (`flock <lock> ./run_tests.sh`): flock(1) keeps the locked descriptor open
    across exec, which is how preflight.sh recognizes itself as the holder.
    `False` is the bare invocation.
    """
    env = {**os.environ, 'PGDATABASE': PROBE_DB, 'V4_WIS_SUITE_LOCK': probe.lock_file}
    argv = ['flock', probe.lock_file, str(PREFLIGHT)] if holding_the_lock else [str(PREFLIGHT)]
    return subprocess.run(argv, cwd=str(CORE_DIR), env=env, capture_output=True, text=True, timeout=120)


class LockHolder:
    """A separate process holding the probe lock — someone who is not an
    ancestor of the preflight run, i.e. a live suite. One process, not
    `flock <file> sleep`, so killing it cannot leave a child behind."""

    def __init__(self, lock_file):
        script = textwrap.dedent(f"""
            import fcntl, sys, time
            fh = open({lock_file!r}, 'w')
            fcntl.flock(fh, fcntl.LOCK_EX)
            sys.stdout.write('locked\\n')
            sys.stdout.flush()
            time.sleep(300)
        """)
        self.proc = subprocess.Popen([sys.executable, '-c', script], stdout=subprocess.PIPE, text=True)
        assert self.proc.stdout.readline().strip() == 'locked'

    def close(self):
        self.proc.kill()
        self.proc.wait(timeout=30)
        self.proc.stdout.close()


def test_the_probe_backends_really_do_look_like_a_killed_run(probe):
    """The precondition every refusal test below depends on: without it, a
    refusal could be refusing something other than what it says."""
    backends = probe.idle_backends(2)
    assert len(backends) == 2
    for _pid, state, query, _idle in backends:
        assert state == 'idle', state
        assert query.strip().rstrip(';').upper() == 'COMMIT', query


def test_refuses_when_nobody_holds_the_suite_lock(probe):
    """Row 47's gate. A bare run cannot claim ownership of anything: the
    backends it sees could be a second bare run's, mid-test. The documented
    invocation always holds the lock, so this costs a correct caller nothing."""
    probe.idle_backends(2)
    result = run_preflight(probe, holding_the_lock=False)

    assert result.returncode == 78, result.stderr
    assert 'nobody holds the suite lock' in result.stderr
    assert f'flock {probe.lock_file} ./run_tests.sh' in result.stderr
    assert probe.wedged_db_exists(), 'a refusal must never drop the database'
    assert len(probe.backends()) == 2, 'a refusal must never terminate a backend'


def test_refuses_when_a_process_that_is_not_an_ancestor_holds_the_suite_lock(probe):
    probe.idle_backends(2)
    holder = LockHolder(probe.lock_file)
    try:
        result = run_preflight(probe, holding_the_lock=False)
    finally:
        holder.close()

    assert result.returncode == 78, result.stderr
    assert 'a live suite is running' in result.stderr
    assert probe.wedged_db_exists()
    assert len(probe.backends()) == 2


def test_refuses_when_a_backend_is_not_idle(probe):
    probe.idle_backends(1)
    probe.backend_in_transaction()
    result = run_preflight(probe, holding_the_lock=True)

    assert result.returncode == 78, result.stderr
    assert 'so something is using it' in result.stderr
    assert probe.wedged_db_exists()
    assert len(probe.backends()) == 2


def test_refuses_when_an_idle_backends_last_statement_was_not_commit(probe):
    probe.backend_idle_after_a_select()
    result = run_preflight(probe, holding_the_lock=True)

    assert result.returncode == 78, result.stderr
    assert 'was not COMMIT' in result.stderr
    assert probe.wedged_db_exists()
    assert len(probe.backends()) == 1


def test_refuses_when_the_backends_have_not_been_idle_long_enough(probe):
    """Freshly committed, so indistinguishable from a live suite between
    tests — which is the whole reason the idle floor exists."""
    probe.idle_backends(2)
    result = run_preflight(probe, holding_the_lock=True)

    assert result.returncode == 78, result.stderr
    assert f'idle for less than {ORPHAN_IDLE_S}s' in result.stderr
    assert probe.wedged_db_exists()
    assert len(probe.backends()) == 2


def test_passes_without_dropping_anything_when_the_test_database_has_no_backends(probe):
    """Left behind but unused: Django drops and recreates it itself, so there
    is nothing here to recover and nothing to refuse."""
    result = run_preflight(probe, holding_the_lock=True)

    assert result.returncode == 0, result.stderr
    assert 'dropped' not in result.stderr
    assert probe.wedged_db_exists()


def test_passes_when_the_test_database_does_not_exist(probe):
    with probe.admin.cursor() as cur:
        _force_drop(cur, PROBE_TEST_DB)
    result = run_preflight(probe, holding_the_lock=True)

    assert result.returncode == 0, result.stderr
    assert not probe.wedged_db_exists()


def test_terminates_and_drops_only_once_every_gate_has_passed(probe):
    """The destructive path, end to end: the lock held, two idle backends whose
    last statement was COMMIT, idle past the floor. Costs one real
    ORPHAN_IDLE_S wait, because that gate is deliberately not overridable."""
    backends = probe.idle_backends(2)
    pids = {b[0] for b in backends}

    # min, not max: the gate requires EVERY backend to be past the floor, so
    # the youngest one is what has to be waited out.
    youngest_idle = min(b[3] for b in probe.backends())
    time.sleep(max(0.0, ORPHAN_IDLE_S - youngest_idle) + 0.5)

    result = run_preflight(probe, holding_the_lock=True)

    assert result.returncode == 0, result.stderr
    assert 'was left behind by a killed run' in result.stderr
    assert f'{PROBE_TEST_DB} dropped' in result.stderr
    assert not probe.wedged_db_exists(), 'the wedged database should be gone, for pytest to recreate'

    with probe.admin.cursor() as cur:
        cur.execute('SELECT pid FROM pg_stat_activity WHERE pid = ANY(%s)', (list(pids),))
        assert cur.fetchall() == [], 'the orphaned backends should have been terminated'


def test_the_lock_ownership_test_recognizes_an_inherited_descriptor(probe):
    """The mechanism the two ownership gates rest on: flock(1) leaves the
    locked descriptor open across exec, so the documented invocation is
    recognizable as the holder. If this stopped being true, every run would
    refuse on the `free` gate instead of recovering, and the refusal message
    would be telling the caller to do what it already did."""
    probe.idle_backends(2)
    with open(probe.lock_file, 'w') as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        # Held by this very process, yet not passed to the child: the child
        # must read the lock as `other`, not as its own.
        result = run_preflight(probe, holding_the_lock=False)
    assert 'a live suite is running' in result.stderr

    # And with flock(1) in front, the same run sees itself as the holder and
    # reaches the idle-floor gate instead.
    result = run_preflight(probe, holding_the_lock=True)
    assert f'idle for less than {ORPHAN_IDLE_S}s' in result.stderr
