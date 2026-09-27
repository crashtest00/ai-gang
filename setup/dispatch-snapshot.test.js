'use strict';

/**
 * Tests for the per-dispatch commons snapshot (V5.0 Agent Commons REQ-02).
 *
 * These cover the snapshot's own mechanics against real directories: the copy,
 * the executable bits, the content hash, the skills replacement, the session
 * environment and the cleanup. They do not stand in for REQ-02's acceptance,
 * which is about what a session started by the subscriber sees and is proved
 * against a running project container.
 *
 * Run through setup/commons/tools/test.sh, which runs this file alongside the
 * artifact helper's tests under the shared suite lock.
 */

const { test, after } = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const os = require('os');
const path = require('path');

const { createSnapshot, installSkills, sessionEnv, removeSnapshot } = require('./dispatch-snapshot');

// A stand-in for the mounted /agent-docs/commons: a tools/ directory with one
// shebang tool and one plain module it requires, and a skills/ directory with
// one skill.
// Every temporary directory these tests mint, removed when the file is done —
// they are real directories under the OS temp dir, not a mocked filesystem.
const scratch = [];
after(() => {
  for (const dir of scratch) fs.rmSync(dir, { recursive: true, force: true });
});

function makeCommons(files = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'commons-src-'));
  scratch.push(dir);
  const contents = {
    'tools/gateway-publish.js': '#!/usr/bin/env node\nconsole.log("published");\n',
    'tools/envelope.js': "'use strict';\nmodule.exports = {};\n",
    'skills/a2a-submit/SKILL.md': '---\nname: a2a-submit\n---\nbody\n',
    ...files,
  };
  for (const [rel, body] of Object.entries(contents)) {
    const file = path.join(dir, rel);
    fs.mkdirSync(path.dirname(file), { recursive: true });
    fs.writeFileSync(file, body, { mode: 0o644 });
  }
  return dir;
}

function makeRoot() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dispatch-root-'));
  scratch.push(dir);
  return dir;
}

test('the snapshot is a full copy of the commons, tools and skills alike', () => {
  const source = makeCommons();
  const root = makeRoot();
  const snapshot = createSnapshot({ source, root });

  assert.strictEqual(
    fs.readFileSync(path.join(snapshot.toolsDir, 'gateway-publish.js'), 'utf8'),
    fs.readFileSync(path.join(source, 'tools/gateway-publish.js'), 'utf8')
  );
  assert.ok(fs.existsSync(path.join(snapshot.commonsDir, 'skills/a2a-submit/SKILL.md')));
  assert.strictEqual(snapshot.toolsDir, path.join(snapshot.commonsDir, 'tools'));
});

test('the snapshot directory is outside the project working tree', () => {
  const source = makeCommons();
  // Default root, which is the value the subscriber uses in the container.
  const snapshot = createSnapshot({ source });
  try {
    assert.ok(!snapshot.dispatchDir.startsWith('/workspace'), snapshot.dispatchDir);
    assert.ok(snapshot.dispatchDir.startsWith(os.tmpdir()), snapshot.dispatchDir);
  } finally {
    removeSnapshot(snapshot);
  }
});

test('a copied file with a shebang is executable and a plain module is not', () => {
  const source = makeCommons();
  const snapshot = createSnapshot({ source, root: makeRoot() });

  const toolMode = fs.statSync(path.join(snapshot.toolsDir, 'gateway-publish.js')).mode & 0o777;
  const moduleMode = fs.statSync(path.join(snapshot.toolsDir, 'envelope.js')).mode & 0o777;
  assert.ok(toolMode & 0o111, `expected executable, got ${toolMode.toString(8)}`);
  assert.strictEqual(moduleMode & 0o111, 0, `expected non-executable, got ${moduleMode.toString(8)}`);
  assert.deepStrictEqual(snapshot.executables, ['tools/gateway-publish.js']);
});

test('the version is a sha256 hex digest and is equal for two snapshots of the same commons', () => {
  const source = makeCommons();
  const first = createSnapshot({ source, root: makeRoot() });
  const second = createSnapshot({ source, root: makeRoot() });

  assert.match(first.version, /^[0-9a-f]{64}$/);
  assert.strictEqual(first.version, second.version);
});

test('changing a file in the commons changes the next snapshot version', () => {
  const source = makeCommons();
  const before = createSnapshot({ source, root: makeRoot() });

  fs.writeFileSync(path.join(source, 'skills/a2a-submit/SKILL.md'), '---\nname: a2a-submit\n---\nchanged\n');
  const after = createSnapshot({ source, root: makeRoot() });

  assert.notStrictEqual(after.version, before.version);
});

test('the version covers file paths, not only contents', () => {
  const a = makeCommons({ 'tools/one.js': "'use strict';\n" });
  const b = makeCommons({ 'tools/two.js': "'use strict';\n" });

  const first = createSnapshot({ source: a, root: makeRoot() });
  const second = createSnapshot({ source: b, root: makeRoot() });

  assert.notStrictEqual(first.version, second.version);
});

test('the version ignores file modes, so a source tree without executable bits still stamps the same', () => {
  const source = makeCommons();
  const bare = createSnapshot({ source, root: makeRoot() });

  fs.chmodSync(path.join(source, 'tools/gateway-publish.js'), 0o755);
  fs.chmodSync(path.join(source, 'tools/envelope.js'), 0o600);
  const remoded = createSnapshot({ source, root: makeRoot() });

  assert.strictEqual(remoded.version, bare.version);
});

test('a running snapshot is unaffected by a later change to the commons', () => {
  const source = makeCommons();
  const snapshot = createSnapshot({ source, root: makeRoot() });
  const asDispatched = fs.readFileSync(path.join(snapshot.toolsDir, 'gateway-publish.js'), 'utf8');
  const stampedVersion = snapshot.version;

  fs.writeFileSync(path.join(source, 'tools/gateway-publish.js'), '#!/usr/bin/env node\nconsole.log("changed");\n');

  assert.strictEqual(fs.readFileSync(path.join(snapshot.toolsDir, 'gateway-publish.js'), 'utf8'), asDispatched);
  assert.strictEqual(snapshot.version, stampedVersion);
});

test('two successive dispatches get different directories and neither reads the other', () => {
  const source = makeCommons();
  const root = makeRoot();
  const first = createSnapshot({ source, root });
  const second = createSnapshot({ source, root });

  assert.notStrictEqual(first.dispatchDir, second.dispatchDir);
  assert.notStrictEqual(first.commonsDir, second.commonsDir);
  assert.notStrictEqual(first.stateDir, second.stateDir);

  // Per-session state one dispatch wrote is not visible to the next.
  fs.writeFileSync(path.join(first.stateDir, 'chain.json'), '{"LAST":"m-1"}');
  assert.deepStrictEqual(fs.readdirSync(second.stateDir), []);

  // And removing the first leaves the second intact.
  removeSnapshot(first);
  assert.ok(!fs.existsSync(first.dispatchDir));
  assert.ok(fs.existsSync(path.join(second.toolsDir, 'gateway-publish.js')));
});

test('per-session state sits beside the snapshot, is writable, and is derivable from AIGANG_COMMONS_DIR', () => {
  const source = makeCommons();
  const snapshot = createSnapshot({ source, root: makeRoot() });
  const env = sessionEnv(snapshot, '/usr/bin');

  const derived = path.join(path.dirname(env.AIGANG_COMMONS_DIR), 'state');
  assert.strictEqual(derived, snapshot.stateDir);
  assert.ok(fs.statSync(snapshot.stateDir).isDirectory());
  fs.writeFileSync(path.join(snapshot.stateDir, 'chain.json'), '{}');
  assert.ok(fs.existsSync(path.join(snapshot.stateDir, 'chain.json')));
  // Beside, not inside.
  assert.ok(!snapshot.stateDir.startsWith(snapshot.commonsDir + path.sep));
});

test('the session environment puts the snapshot tools first on PATH and stamps the version', () => {
  const source = makeCommons();
  const snapshot = createSnapshot({ source, root: makeRoot() });
  const env = sessionEnv(snapshot, '/usr/local/bin:/usr/bin');

  assert.strictEqual(env.PATH, `${snapshot.toolsDir}:/usr/local/bin:/usr/bin`);
  assert.strictEqual(env.PATH.split(':')[0], snapshot.toolsDir);
  assert.strictEqual(env.AIGANG_COMMONS_DIR, snapshot.commonsDir);
  assert.strictEqual(env.AIGANG_COMMONS_VERSION, snapshot.version);
});

test('installing skills replaces whatever was in the skills directory', () => {
  const source = makeCommons();
  const snapshot = createSnapshot({ source, root: makeRoot() });
  const skillsHome = path.join(makeRoot(), '.claude', 'skills');

  // A skill left behind by an earlier dispatch, and a stale file beside it.
  fs.mkdirSync(path.join(skillsHome, 'retired-skill'), { recursive: true });
  fs.writeFileSync(path.join(skillsHome, 'retired-skill', 'SKILL.md'), 'old\n');

  installSkills(snapshot, skillsHome);

  assert.deepStrictEqual(fs.readdirSync(skillsHome).sort(), ['a2a-submit']);
  assert.strictEqual(
    fs.readFileSync(path.join(skillsHome, 'a2a-submit', 'SKILL.md'), 'utf8'),
    fs.readFileSync(path.join(snapshot.commonsDir, 'skills/a2a-submit/SKILL.md'), 'utf8')
  );
});

test('installing skills creates the skills directory when there is none yet', () => {
  const source = makeCommons();
  const snapshot = createSnapshot({ source, root: makeRoot() });
  const skillsHome = path.join(makeRoot(), 'fresh-home', '.claude', 'skills');

  installSkills(snapshot, skillsHome);

  assert.deepStrictEqual(fs.readdirSync(skillsHome), ['a2a-submit']);
});

test('a commons with no skills directory leaves an empty skills directory', () => {
  const source = makeCommons();
  fs.rmSync(path.join(source, 'skills'), { recursive: true });
  const snapshot = createSnapshot({ source, root: makeRoot() });
  const skillsHome = path.join(makeRoot(), '.claude', 'skills');
  fs.mkdirSync(path.join(skillsHome, 'retired-skill'), { recursive: true });

  installSkills(snapshot, skillsHome);

  assert.deepStrictEqual(fs.readdirSync(skillsHome), []);
});

test('removing the snapshot removes the snapshot and the state beside it', () => {
  const source = makeCommons();
  const snapshot = createSnapshot({ source, root: makeRoot() });
  fs.writeFileSync(path.join(snapshot.stateDir, 'chain.json'), '{}');

  removeSnapshot(snapshot);

  assert.ok(!fs.existsSync(snapshot.dispatchDir));
  assert.ok(!fs.existsSync(snapshot.commonsDir));
  assert.ok(!fs.existsSync(snapshot.stateDir));
  // The mounted commons it was copied from is untouched.
  assert.ok(fs.existsSync(path.join(source, 'tools/gateway-publish.js')));
});
