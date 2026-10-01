"""conftest.py's DatabaseTeardownGuard: the run that cannot drop its own test
database fails, instead of warning and exiting 0 while the next run inherits the
wedge (V5.1 audit row 32).

The guard is driven directly here, with the warning pytest-django actually
emits. The other half of the evidence is a real wedge: outside connections held
on test_workitem for the life of a run, which makes Django's drop fail for real
— see tests/test_preflight.py for the same technique, and the audit row for the
reproduction. That cannot be a test in this suite, because the suite is the
thing whose session outcome it would be asserting about.
"""

import warnings

import psycopg2
import pytest

from conftest import EXIT_TEST_DATABASE_LEFT_BEHIND

GUARD_PLUGIN = 'aigang-test-database-teardown-guard'


def _teardown_warning():
    """The warning pytest-django emits when the drop fails, built the way it
    builds it: `pytest.PytestWarning(f'...: {exc!r}')` over the exception
    Postgres raises when backends are still on the database."""
    exc = psycopg2.OperationalError('database "test_workitem" is being accessed by other users')
    return pytest.PytestWarning(f'Error when trying to teardown test databases: {exc!r}')


def _recorded(warning):
    return warnings.WarningMessage(warning, type(warning), __file__, 0)


class _Session:
    """Everything pytest_sessionfinish touches, and nothing else."""

    def __init__(self, exitstatus=0):
        self.exitstatus = exitstatus


def _fresh_guard(pytestconfig):
    """A guard of the registered class, with its own empty state — so a test
    can never make the guard watching this very run think it saw a failure."""
    live = pytestconfig.pluginmanager.get_plugin(GUARD_PLUGIN)
    assert live is not None, f'conftest.pytest_configure did not register {GUARD_PLUGIN}'
    return type(live)()


def test_the_guard_is_registered_for_this_run(pytestconfig):
    live = pytestconfig.pluginmanager.get_plugin(GUARD_PLUGIN)
    assert live is not None
    assert live.failures == [], 'this run has already failed to drop a test database'


def test_a_teardown_failure_fails_an_otherwise_passing_session(pytestconfig, capsys):
    guard = _fresh_guard(pytestconfig)
    session = _Session(exitstatus=0)

    guard.pytest_warning_recorded(_recorded(_teardown_warning()), 'runtest', '', None)
    guard.pytest_sessionfinish(session, session.exitstatus)

    assert session.exitstatus == EXIT_TEST_DATABASE_LEFT_BEHIND
    err = capsys.readouterr().err
    # What the run that caused it has to be told: which database, what Postgres
    # said, and how to find what is holding it.
    assert 'test_workitem' in err
    assert 'is being accessed by other users' in err
    assert 'pg_stat_activity' in err


def test_a_clean_teardown_leaves_the_session_alone(pytestconfig, capsys):
    guard = _fresh_guard(pytestconfig)
    session = _Session(exitstatus=0)

    guard.pytest_sessionfinish(session, session.exitstatus)

    assert session.exitstatus == 0
    assert capsys.readouterr().err == ''


def test_an_unrelated_warning_is_not_a_wedge(pytestconfig, capsys):
    """The targeted check, and the reason it is not `-W error::pytest.Pytest\
Warning`: every other PytestWarning the suite can emit stays a warning."""
    guard = _fresh_guard(pytestconfig)
    session = _Session(exitstatus=0)

    guard.pytest_warning_recorded(
        _recorded(pytest.PytestWarning('cannot collect test class Foo because it has a __init__ constructor')),
        'collect', 'tests/test_something.py', None)
    # Nor does the text alone, carried by some other warning category.
    guard.pytest_warning_recorded(
        _recorded(UserWarning('Error when trying to teardown test databases: not pytest-django')),
        'runtest', '', None)
    guard.pytest_sessionfinish(session, session.exitstatus)

    assert guard.failures == []
    assert session.exitstatus == 0
    assert capsys.readouterr().err == ''


def test_failing_tests_keep_their_own_exit_code(pytestconfig, capsys):
    """A run that failed tests *and* left the database behind reports the test
    failures: that is the more important result, and 79 would hide it. The
    diagnosis is printed either way."""
    guard = _fresh_guard(pytestconfig)
    session = _Session(exitstatus=1)

    guard.pytest_warning_recorded(_recorded(_teardown_warning()), 'runtest', '', None)
    guard.pytest_sessionfinish(session, session.exitstatus)

    assert session.exitstatus == 1
    assert 'test_workitem' in capsys.readouterr().err
