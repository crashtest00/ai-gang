#!/usr/bin/env bash
# The container-side suite. Four test files:
#
#   request-artifact.test.js      the artifact helper, against real Redis
#   a2a-validate.test.js          construction-time validation on the raw
#                                 file/stdin entry point (gateway-publish.js)
#   a2a-submit.test.js            the constructor an agent invokes (a2a-submit.js)
#   ../../dispatch-snapshot.test.js  the per-dispatch commons snapshot
#
# The two a2a files run their tool as a real child process against the same
# test Redis, because that is the path a producer invokes; each writes only to
# a gateway stream name of its own and deletes it, rather than flushing a
# database it shares.
#
# The last one lives in setup/ rather than here because the module it covers
# does: setup/dispatch-snapshot.js is the container runtime's, not something an
# agent runs, so it stays out of the commons it snapshots and out of the hash it
# computes. It is run from here because this is the container-side suite, and
# adding a fifth suite to the four the build verifies would hide it.
#
# Runs every test that needs Redis against the real test Redis
# (services/core/docker-compose.test.yml,
# redis://localhost:16399 — start it first if it is not already up; never
# `down` it). Holds the same suite lock
# services/core/run_tests.sh's caller does
# (flock /tmp/v4-wis-suite.lock ./run_tests.sh -q) rather than relying on
# the caller to remember it: this file's tests write to the real
# aigang:librarian:requests/:responses stream names a live librarian
# consumer also reads, so it must never run concurrently with that suite
# against the same shared test Redis container (V4 audit Pass 2 row 35).
set -euo pipefail
cd "$(dirname "$0")"

LOCK_FILE="${V4_WIS_SUITE_LOCK:-/tmp/v4-wis-suite.lock}"
export NODE_PATH="${NODE_PATH:-$(npm root -g)}"

# A caller that wraps this script in `flock` on the same path deadlocks it
# against its own parent: flock(1) leaves the locked descriptor open across
# exec, so the lock is already held by an ancestor and the acquisition below
# can never succeed. The wait is invisible — no output, near-zero CPU, forever
# — and it cost this build two killed runs and seven minutes before the cause
# was visible (V5.1 audit row 2). Make it an error instead.
#
# A bare `flock -n` first cannot do this job: a failed non-blocking
# acquisition does not say who holds the lock, and when the holder is another
# suite this script must block — serialising against that suite is the only
# reason it holds a lock at all. An inherited descriptor identifies the
# self-deadlock exactly and leaves every other case to the blocking flock
# below, which still behaves as it always has.
inherited_lock_fd() {
  local target fd
  target="$(readlink -f -- "$1" 2>/dev/null || true)"
  [ -n "$target" ] || return 1
  for fd in /proc/self/fd/*; do
    case "${fd##*/}" in 0 | 1 | 2) continue ;; esac
    if [ "$(readlink -f -- "$fd" 2>/dev/null || true)" = "$target" ]; then
      printf '%s\n' "${fd##*/}"
      return 0
    fi
  done
  return 1
}

if inherited_fd="$(inherited_lock_fd "$LOCK_FILE")"; then
  cat >&2 <<EOF

$(basename "$0"): refusing to run. The suite lock $LOCK_FILE is already held by
an ancestor of this process. It arrived here as inherited file descriptor
number $inherited_fd, which is how flock(1) hands a held lock to the command it runs.

This script takes that lock itself, deliberately, so a caller cannot forget it
(see the header above). Wrapping it in flock on the same path therefore makes it
wait for a lock only its own caller can release, which never happens: the hang
you would have seen is this, not a slow test.

Invoke it bare:

    setup/commons/tools/test.sh

The flock wrapper belongs to services/core/run_tests.sh, whose caller does have
to supply it. This script does not.
EOF
  exit 78
fi

# --- the npm packages this suite's requires resolve to ----------------------
#
# Three of the four test files, and the tools they run as child processes,
# `require('redis')` — not a builtin and not relative, so it resolves through
# NODE_PATH above, to a global npm install. When it is not there, `node --test`
# reports MODULE_NOT_FOUND as `failureType: testCodeFailure`: three failures
# that look exactly like test failures and are not, with the real cause buried
# in a require stack above them (V5.1 audit rows 31 and 49). Reproduced rather
# than inferred — `NODE_PATH=/an/empty/dir setup/commons/tools/test.sh` returns
# `# fail 3`.
#
# The set is read out of the files rather than restated here, so a module added
# to any of them is covered without this check having to be remembered into.
missing="$(node <<'JS'
const fs = require('node:fs');
const path = require('node:path');
const { isBuiltin } = require('node:module');

// Exactly the files this suite loads: every .js in this directory (the three
// test files and the tools they run as child processes), plus the fourth test
// file and its module, which live in setup/. Not all of setup/ — subscriber.js
// is the container runtime's and nothing here loads it, so a dependency of its
// own must not be able to refuse this suite.
const files = fs.readdirSync('.').filter((entry) => entry.endsWith('.js'))
  .concat([path.join('..', '..', 'dispatch-snapshot.js'), path.join('..', '..', 'dispatch-snapshot.test.js')]);

const names = new Set();
for (const entry of files) {
  const source = fs.readFileSync(entry, 'utf8');
  for (const m of source.matchAll(/require\(\s*(['"])([^'"]+)\1\s*\)/g)) {
    const name = m[2];
    if (name.startsWith('.') || name.startsWith('/') || isBuiltin(name)) continue;
    names.add(name.split('/')[0]);   // the package, not the subpath
  }
}

const missing = [...names].sort().filter((name) => {
  try { require.resolve(name); return false; } catch { return true; }
});
process.stdout.write(missing.join(' '));
JS
)"

if [ -n "$missing" ]; then
  cat >&2 <<EOF

$(basename "$0"): refusing to run. These npm package(s) the suite requires do not
resolve under NODE_PATH=$NODE_PATH:

    $missing

node --test would report that as failures of type testCodeFailure, which is what
it is not: no test ran. Install them the way the project containers do (Docker
Templates/Dockerfile-node.template), then re-run this script:

    sudo npm install -g $missing
    setup/commons/tools/test.sh

If they are installed somewhere else, point NODE_PATH at that directory instead;
unset, this script uses the output of \`npm root -g\`.
EOF
  exit 78
fi

flock "$LOCK_FILE" node --test \
  request-artifact.test.js \
  a2a-validate.test.js \
  a2a-submit.test.js \
  ../../dispatch-snapshot.test.js
