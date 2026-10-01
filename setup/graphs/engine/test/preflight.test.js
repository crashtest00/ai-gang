'use strict';

// preflight.js, the `pretest` hook on this package's own `npm test`: the check
// that a fresh worktree's missing node_modules is reported as what it is,
// rather than as seven test files failing with MODULE_NOT_FOUND (V5.1 audit
// row 31).
//
// Driven as a real child process, because its exit code is the whole point:
// npm runs `pretest` before `test` and stops on a non-zero exit, so a code
// outside node --test's own 0/1 is what makes a refusal unmistakable and what
// keeps the suite from running at all.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const ENGINE_DIR = path.join(__dirname, '..');
const PREFLIGHT = path.join(ENGINE_DIR, 'preflight.js');

function run(packageDir) {
  return spawnSync(process.execPath, packageDir ? [PREFLIGHT, packageDir] : [PREFLIGHT], {
    encoding: 'utf8',
    timeout: 20000,
    cwd: ENGINE_DIR,
  });
}

// A package directory under the OS temporary directory, so the dependency
// resolution preflight.js performs walks /tmp's ancestors and not this
// repository's — a dependency installed here must not make a temporary
// package's missing one look present.
function makePackage({ dependencies = {}, devDependencies = {}, installed = [], scripts = undefined } = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'aigang-engine-preflight-'));
  fs.writeFileSync(path.join(dir, 'package.json'), JSON.stringify({
    name: 'preflight-fixture', version: '0.0.0', private: true, dependencies, devDependencies, scripts,
  }));
  for (const name of installed) {
    const moduleDir = path.join(dir, 'node_modules', name);
    fs.mkdirSync(moduleDir, { recursive: true });
    fs.writeFileSync(path.join(moduleDir, 'package.json'), JSON.stringify({ name, version: '0.0.0', main: 'index.js' }));
    fs.writeFileSync(path.join(moduleDir, 'index.js'), 'module.exports = {};\n');
  }
  return dir;
}

test('a declared dependency that does not resolve refuses the run with EX_CONFIG', () => {
  const dir = makePackage({ dependencies: { 'js-yaml': '^4.1.0' } });
  const result = run(dir);

  assert.equal(result.status, 78, result.stderr);
  assert.equal(result.stdout, '');
  assert.match(result.stderr, /js-yaml/);
  // The two things the refusal exists to say: that this is not a test result,
  // and the one command that fixes it.
  assert.match(result.stderr, /testCodeFailure/);
  assert.match(result.stderr, /npm ci/);
  assert.ok(result.stderr.includes(`cd ${dir} && npm ci`), result.stderr);
});

test('every unresolvable dependency is named, from both dependency fields', () => {
  // The set is read out of package.json rather than restated in preflight.js,
  // which is what keeps a dependency added later covered. Resolving it from
  // both fields is part of that: a devDependency is required by the test files
  // just as hard as a dependency is.
  const dir = makePackage({
    dependencies: { 'js-yaml': '^4.1.0', 'not-a-real-package': '^1.0.0' },
    devDependencies: { 'also-not-real': '^1.0.0' },
  });
  const result = run(dir);

  assert.equal(result.status, 78);
  for (const name of ['js-yaml', 'not-a-real-package', 'also-not-real']) {
    assert.match(result.stderr, new RegExp(name));
  }
});

test('a package whose declared dependencies all resolve is silent and exits 0', () => {
  const dir = makePackage({
    dependencies: { 'a-fixture-dependency': '^1.0.0' },
    installed: ['a-fixture-dependency'],
  });
  const result = run(dir);

  assert.equal(result.status, 0, result.stderr);
  assert.equal(result.stdout, '');
  assert.equal(result.stderr, '');
});

test('a package that declares no dependency at all exits 0', () => {
  const result = run(makePackage());
  assert.equal(result.status, 0, result.stderr);
  assert.equal(result.stderr, '');
});

test('this package is itself prepared — the suite now running proves it, and so does the preflight', () => {
  // Not vacuous: this is the check `npm test` just ran as its `pretest`, over
  // the real engine package, and the suite could not have reached this file if
  // js-yaml were absent. It fails if the two ever disagree — a dependency
  // declared in package.json but absent from node_modules while the test files
  // happen not to require it yet.
  const result = run();
  assert.equal(result.status, 0, result.stderr);
});

test('npm test runs the preflight before the suite and stops on its refusal', () => {
  // The wiring, not the check: `pretest` is what makes the documented
  // invocation (`npm test`) carry the preflight with no wrapper script, and npm
  // propagating its exit code unchanged — rather than running `test` anyway or
  // reporting its own 1 — is what makes the refusal visible.
  const dir = makePackage({
    dependencies: { 'not-a-real-package': '^1.0.0' },
    scripts: {
      pretest: `${JSON.stringify(process.execPath)} ${JSON.stringify(PREFLIGHT)} .`,
      test: 'echo THE-SUITE-RAN',
    },
  });

  const result = spawnSync('npm', ['test'], { cwd: dir, encoding: 'utf8', timeout: 120000 });
  assert.equal(result.status, 78, `${result.stdout}${result.stderr}`);
  assert.doesNotMatch(`${result.stdout}${result.stderr}`, /THE-SUITE-RAN/);
  assert.match(result.stderr, /not-a-real-package/);
});
