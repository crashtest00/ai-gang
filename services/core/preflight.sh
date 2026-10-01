#!/usr/bin/env bash
# run_tests.sh's preflight: turns the three ways a worktree can be unprepared,
# and the one way a killed run wedges the shared test database, into named
# errors that say what to run — instead of the failures they actually produce,
# none of which resembles a test result (V5.1 audit rows 3 and 4):
#
#   exit 127  `.venv/bin/python: No such file or directory`, the gitignored
#             virtualenv a fresh worktree never has
#   exit 4    conftest.py's `Static files haven't been collected`, the
#             gitignored staticfiles/ manifest
#   db setup  `database "test_workitem" already exists`, then `is being
#             accessed by other users` — a run killed mid-test leaves backends
#             connected to the test database, and Django's own
#             drop-and-recreate cannot get past them
#
# Setup is diagnosed, never performed. Creating a virtualenv and installing
# requirements costs minutes and reaches the network, and this script runs
# inside the caller's `timeout` bound and under the shared suite lock: setup
# spent from that budget is setup charged to a test run, and a bound that
# expires mid-test is exactly what orphaned a consumer for 3h37m (audit row
# 11). A test command's job is to report, and its exit code should mean a test
# result or a named refusal, never "I changed your tree".
#
# The wedged test database IS recovered automatically, because it can be made
# safe: see `suite_lock_state` and the four gates below. Every gate that
# cannot be proven refuses, prints the backends it found and gives the exact
# recovery commands instead.
#
# Run through run_tests.sh, which exports the PG*/REDIS* environment this
# needs; running it bare stops on PGDATABASE with that instruction.
set -euo pipefail
cd "$(dirname "$0")"

# sysexits.h EX_CONFIG. Outside pytest's own 0-5, so a preflight refusal can
# never be read as a test outcome.
EX_PREFLIGHT=78
LOCK_FILE="${V4_WIS_SUITE_LOCK:-/tmp/v4-wis-suite.lock}"
# How long a backend must have been idle before it can be called an orphan.
# A live suite between tests is idle for milliseconds; a killed one is idle
# for as long as it has been dead.
ORPHAN_IDLE_S=10

fail() { printf '\nrun_tests.sh preflight: %s\n' "$1" >&2; }

setup_steps() {
  cat >&2 <<'EOF'

The three setup steps, from services/core:

    python3 -m venv .venv
    .venv/bin/python -m pip install -r requirements.txt
    .venv/bin/python manage.py collectstatic --noinput

Then re-run the suite. Use `.venv/bin/python -m pip`, not `.venv/bin/pip`:
a virtualenv created under a path that has since moved keeps the old absolute
path in its console-script shebangs, so `.venv/bin/pip` fails with "No such
file or directory" while the interpreter beside it works (audit row 1).
EOF
}

# --- 1. the virtualenv (gitignored, so absent in every fresh worktree) ------
if [ ! -x .venv/bin/python ]; then
  fail "this worktree has no virtualenv — $PWD/.venv/bin/python is missing."
  setup_steps
  exit "$EX_PREFLIGHT"
fi

# --- 2. the requirements in it ---------------------------------------------
missing="$(.venv/bin/python - <<'PY'
import importlib.util
# Every import the suite reaches before any test body: pytest and its Django
# plugin, Django itself, the middleware conftest.py already checks for, the
# Postgres driver this script needs below, and the two clients the tests use.
names = ('pytest', 'pytest_django', 'django', 'whitenoise', 'psycopg2', 'redis', 'requests', 'dotenv')
print(' '.join(n for n in names if importlib.util.find_spec(n) is None))
PY
)" || {
  fail "this worktree's .venv/bin/python exists but cannot run — the virtualenv is broken."
  setup_steps
  exit "$EX_PREFLIGHT"
}
if [ -n "$missing" ]; then
  fail "this worktree's virtualenv is missing requirements.txt packages: $missing"
  setup_steps
  exit "$EX_PREFLIGHT"
fi

# --- 3. the collected static files (gitignored too) ------------------------
if [ ! -f staticfiles/staticfiles.json ]; then
  fail "static files have not been collected — $PWD/staticfiles/staticfiles.json is missing, and conftest.py stops the run with pytest's exit 4 rather than running a test."
  setup_steps
  exit "$EX_PREFLIGHT"
fi

# --- 4. the shared test database -------------------------------------------
#
# Who legitimately owns the test database: whoever holds the suite lock, which
# is exclusive, so there is at most one. flock(1) leaves the locked descriptor
# open across exec, so a run invoked the documented way
# (`flock /tmp/v4-wis-suite.lock ./run_tests.sh`) inherits it and can see that
# it is the holder — which is the whole distinction row 4 turns on:
#
#   ours   our own caller holds the lock. We are the legitimate run; nobody
#          else can be, so any other backend on the test database is a leftover
#   free   nobody holds it. There is no legitimate run to protect
#   other   someone who is not an ancestor of ours holds it: a live suite.
#          Refuse, and say so — its backends are not ours to kill
suite_lock_state() {
  local target fd
  target="$(readlink -f -- "$LOCK_FILE" 2>/dev/null || true)"
  if [ -n "$target" ]; then
    for fd in /proc/self/fd/*; do
      case "${fd##*/}" in 0 | 1 | 2) continue ;; esac
      if [ "$(readlink -f -- "$fd" 2>/dev/null || true)" = "$target" ]; then
        printf 'ours\n'
        return 0
      fi
    done
  fi
  if flock -n "$LOCK_FILE" true 2>/dev/null; then printf 'free\n'; else printf 'other\n'; fi
}

PREFLIGHT_LOCK_STATE="$(suite_lock_state)" \
PREFLIGHT_LOCK_FILE="$LOCK_FILE" \
PREFLIGHT_ORPHAN_IDLE_S="$ORPHAN_IDLE_S" \
PREFLIGHT_TEST_DB="test_${PGDATABASE:?preflight.sh needs the suite environment — run it through ./run_tests.sh, which exports PGDATABASE and the rest}" \
  .venv/bin/python - <<'PY' || exit "$EX_PREFLIGHT"
import os
import sys
import time

import psycopg2

TEST_DB = os.environ['PREFLIGHT_TEST_DB']
LOCK_STATE = os.environ['PREFLIGHT_LOCK_STATE']
LOCK_FILE = os.environ['PREFLIGHT_LOCK_FILE']
IDLE_S = int(os.environ['PREFLIGHT_ORPHAN_IDLE_S'])
PGHOST, PGPORT = os.environ['PGHOST'], os.environ['PGPORT']

# clock_timestamp(), not now(): now() is the transaction timestamp, so it is
# frozen for the life of a transaction and the idle times below would all be
# measured from the same instant.
BACKENDS = """
    SELECT pid, state, coalesce(query, ''),
           extract(epoch FROM (clock_timestamp() - state_change))
      FROM pg_stat_activity
     WHERE datname = %s AND pid <> pg_backend_pid()
"""


def refuse(reason, backends):
    print(f'\nrun_tests.sh preflight: {reason}', file=sys.stderr)
    if backends:
        print(f'\nBackends on {TEST_DB} right now:', file=sys.stderr)
        for pid, state, query, idle in backends:
            print(f'    pid {pid:<8} {state or "?":<20} idle {idle or 0:.0f}s    last statement: {query!r}',
                  file=sys.stderr)
    print(f"""
Recovery, once you have established that no suite you care about is running
(the suite lock is {LOCK_FILE}):

    docker exec -i workitem-test-postgres-django psql -U workitem -d workitem -c \\
      "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{TEST_DB}';"
    docker exec -i workitem-test-postgres-django psql -U workitem -d workitem -c \\
      "DROP DATABASE IF EXISTS {TEST_DB};"

pytest recreates {TEST_DB} on the next run. Dropping it loses nothing: it is
built from migrations at the start of every run and is not the development
database, which is "{os.environ['PGDATABASE']}" on the same instance.""", file=sys.stderr)
    sys.exit(1)


try:
    conn = psycopg2.connect(host=PGHOST, port=PGPORT, user=os.environ['PGUSER'],
                            password=os.environ['PGPASSWORD'], dbname=os.environ['PGDATABASE'])
except psycopg2.OperationalError as exc:
    # Not row 4, but this check is the first thing to touch Postgres, so a
    # stopped test container would otherwise meet a traceback here instead of
    # pytest's own message.
    print(f'\nrun_tests.sh preflight: cannot reach the test Postgres at {PGHOST}:{PGPORT} '
          f'({str(exc).strip()}). Start the test containers first, from services/core:\n\n'
          f'    docker compose -f docker-compose.test.yml up --wait\n', file=sys.stderr)
    sys.exit(1)
conn.autocommit = True
# `with conn.cursor()`, deliberately not `with conn, conn.cursor()`: psycopg2's
# connection context manager opens a transaction for the whole block even under
# autocommit, which pins one MVCC snapshot — and then pg_stat_activity never
# changes inside it, so the poll below would watch terminated backends appear to
# live forever and this script would refuse a recovery it had already completed.
with conn.cursor() as cur:
    cur.execute('SELECT 1 FROM pg_database WHERE datname = %s', (TEST_DB,))
    if cur.fetchone() is None:
        sys.exit(0)

    cur.execute(BACKENDS, (TEST_DB,))
    backends = cur.fetchall()
    if not backends:
        # Left behind but unused. Django drops and recreates it itself with
        # interactive=False; only connections stop that, so there is nothing
        # here to fix.
        sys.exit(0)

    if LOCK_STATE == 'other':
        refuse(f'{TEST_DB} has {len(backends)} other connection(s), and a process that is not an '
               f'ancestor of this one holds the suite lock {LOCK_FILE} — so a live suite is running '
               f'and these are most likely its backends. Not touching them.', backends)

    busy = [b for b in backends if b[1] != 'idle']
    if busy:
        refuse(f'{TEST_DB} has {len(busy)} connection(s) that are not idle '
               f'({", ".join(sorted({b[1] or "?" for b in busy}))}), so something is using it. '
               f'An orphan from a killed run is idle; this is not that.', backends)

    unclean = [b for b in backends if b[2].strip().rstrip(';').upper() != 'COMMIT']
    if unclean:
        refuse(f'{TEST_DB} has {len(unclean)} idle connection(s) whose last statement was not COMMIT, '
               f'so the work they did may not have been committed and they may not be orphans.', backends)

    fresh = [b for b in backends if (b[3] or 0) < IDLE_S]
    if fresh:
        refuse(f'{TEST_DB} has {len(fresh)} connection(s) idle for less than {IDLE_S}s. A live suite '
               f'between tests looks exactly like this, so these cannot be called orphans yet. '
               f'Wait {IDLE_S}s and re-run.', backends)

    pids = [b[0] for b in backends]
    print(f'run_tests.sh preflight: {TEST_DB} was left behind by a killed run — '
          f'{len(pids)} idle backend(s) {pids}, last statement COMMIT, idle '
          f'{min(b[3] or 0 for b in backends):.0f}s or more, and no live suite holds {LOCK_FILE} '
          f'(lock state: {LOCK_STATE}). Terminating them and dropping {TEST_DB}; '
          f'pytest recreates it.', file=sys.stderr)
    cur.execute('SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s', (TEST_DB,))

    deadline = time.time() + 10
    while time.time() < deadline:
        cur.execute(BACKENDS, (TEST_DB,))
        remaining = cur.fetchall()
        if not remaining:
            break
        time.sleep(0.2)
    else:
        refuse(f'terminated the backends on {TEST_DB} but {len(remaining)} is/are still connected '
               f'after 10s, so the database still cannot be dropped.', remaining)

    cur.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}"')
    print(f'run_tests.sh preflight: {TEST_DB} dropped. Running the suite.', file=sys.stderr)
PY
