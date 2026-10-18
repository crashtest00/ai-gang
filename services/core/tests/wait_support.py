"""
The one `wait_for` the Streams/relay tests poll with.

Not collected by pytest (pytest.ini's `python_files = test_*.py`); imported by
the test modules that drive a real consumer, relay or webhook batch and have to
wait for it. It lives beside `artifacts_support.py` and `librarian_support.py`
rather than in the service-wide conftest.py because conftest.py is where
fixtures go, and importing a plain function out of a conftest module works only
through pytest's rootdir sys.path insertion, where `tests/` is already a real
package every test module imports from.

**Why one copy.** Six test modules each had their own `wait_for`, five of them
raising a bare `TimeoutError('wait_for timed out')` (V5.1 audit rows 28 and 30).
That message names neither what was being waited for nor what was actually
there, so the first diagnosis of a real failure goes to the wrong place: it
reads as too short a deadline, when "observed 4/6 created" would have pointed
straight at a second consumer in the group stealing two of the six deliveries
(rows 5 and 11). Hoisting the capable signature here makes the diagnostic the
default for every call site, present and future, instead of something five
files have to be remembered into.
"""

from __future__ import annotations

import time

__all__ = ['wait_for']


def wait_for(predicate, timeout_s=5.0, interval_s=0.03, expected=None, observed=None):
    """Polls `predicate` until it holds or `timeout_s` elapses.

    `expected` names, in words, the state being waited for, and `observed` is a
    zero-argument callable sampled only when the wait fails, so the message
    carries the value the predicate was looking at.

    The defaults are the short ones (5s at 30ms) the command/webhook-consumer
    tests use; the two modules that kill a consumer or relay mid-batch pass the
    longer 20s/100ms pair explicitly at each call site, so no caller's effective
    deadline depends on which module the helper happens to be read from.
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(interval_s)
    message = f'wait_for timed out after {timeout_s}s waiting for: {expected or "<unlabelled predicate>"}'
    if observed is not None:
        try:
            message += f' — observed: {observed()}'
        except Exception as exc:  # pragma: no cover — a broken sampler must not hide the timeout
            message += f' — observed: unavailable, the sampler raised {exc!r}'
    raise TimeoutError(message)
