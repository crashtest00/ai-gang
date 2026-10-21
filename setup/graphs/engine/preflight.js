#!/usr/bin/env node
'use strict';

/**
 * `npm test`'s preflight for this package: turns the one way a fresh worktree
 * is unprepared into a named error that says what to run, instead of the
 * failures it actually produces (V5.1 audit row 31).
 *
 * `node_modules/` is gitignored, so every fresh worktree starts without it and
 * nothing in `git clone` or `git worktree add` creates it. The suite's files
 * `require('js-yaml')` — not a builtin and not relative — so without it seven
 * test files throw MODULE_NOT_FOUND at load time, and `node --test` reports a
 * file that failed to load as a *test* failure: `# fail 7`, each one
 * `failureType: 'testCodeFailure'`, with the real cause buried in a require
 * stack. Two agents in this build read that as seven broken tests. No test ran.
 *
 * Wired as npm's `pretest`, so the documented invocation
 * (`cd setup/graphs/engine && flock /tmp/v4-wis-suite.lock npm test`) gets it
 * without a wrapper script and without the caller having to remember it. A bare
 * `node --test` bypasses it, as it bypasses every `npm test` hook; it also
 * matches no test-file pattern itself (`preflight.js` is not `test-*.js`,
 * `*.test.js` or under `test/`), so running the suite does not run it as a test.
 *
 * `pretest` is already this repository's mechanism for a suite precondition:
 * `services/scrummaster/scripts/check-test-redis.js` is one, for a Redis the
 * tests dial. It exits 1; this exits 78, because the failure it is standing in
 * front of is one that already looks like a test result, and 1 is what
 * `node --test` returns for real test failures.
 *
 * The required set is read out of `package.json`'s own dependency fields rather
 * than restated here, so a dependency added to this package is covered without
 * this check having to be remembered into — the property
 * `setup/commons/tools/test.sh`'s equivalent has for its own global installs.
 * The two differ in their fix because they differ in their mechanism: the
 * commons tools resolve through a global install (`NODE_PATH`), and this package
 * declares its dependencies, so here `npm ci` is the whole answer.
 *
 * Setup is diagnosed, never performed: `npm ci` reaches the network and this
 * runs inside the caller's `timeout` bound and under the shared suite lock, so
 * an install it did itself would be setup charged to a test run (the reasoning
 * in services/core/preflight.sh's header, which this follows).
 *
 * Checks the package in `process.argv[2]` when given one, so its own test can
 * drive it against a temporary package; defaults to this one.
 */

const path = require('node:path');

// sysexits.h EX_CONFIG, the code services/core/preflight.sh refuses with.
// Outside `node --test`'s own 0/1, so a refusal can never be read as a test
// outcome, and npm exits with it unchanged and does not run `test`.
const EX_PREFLIGHT = 78;

const packageDir = path.resolve(process.argv[2] || __dirname);
const manifest = require(path.join(packageDir, 'package.json'));

const declared = [...new Set([
  ...Object.keys(manifest.dependencies || {}),
  ...Object.keys(manifest.devDependencies || {}),
])].sort();

// Resolved from the package directory, which is how node resolves the requires
// in its files: `<packageDir>/node_modules` first, then every ancestor's, then
// NODE_PATH. So this answers the question the suite is about to ask, rather
// than a different one about where the files happen to be.
const missing = declared.filter((name) => {
  try {
    require.resolve(name, { paths: [packageDir] });
    return false;
  } catch {
    return true;
  }
});

if (missing.length) {
  process.stderr.write(`
preflight.js: refusing to run. ${manifest.name} declares dependencies that do not
resolve from ${packageDir}:

    ${missing.join(' ')}

node --test would report that as test files failing with MODULE_NOT_FOUND, of
failureType 'testCodeFailure', which is what it is not: no test ran. This
worktree has no node_modules, or an incomplete one. Install it:

    cd ${packageDir} && npm ci

Then re-run the suite. npm ci installs exactly the tree package-lock.json
records, and changes nothing tracked.
`);
  process.exit(EX_PREFLIGHT);
}
