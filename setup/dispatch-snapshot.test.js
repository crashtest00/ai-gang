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

const { createSnapshot, installSkills, sessionEnv, removeSnapshot, pruneSnapshotRoot } = require('./dispatch-snapshot');

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

test('an empty directory and an absent one stamp differently', () => {
  // installSkills' own existsSync branch behaves differently for the two, so a
  // stamp that could not tell them apart would call two sessions that ran
  // differently the same commons.
  const withEmpty = makeCommons();
  fs.rmSync(path.join(withEmpty, 'skills/a2a-submit'), { recursive: true });
  const without = makeCommons();
  fs.rmSync(path.join(without, 'skills'), { recursive: true });

  const empty = createSnapshot({ source: withEmpty, root: makeRoot() });
  const absent = createSnapshot({ source: without, root: makeRoot() });

  assert.deepStrictEqual(fs.readdirSync(path.join(empty.commonsDir, 'skills')), [],
    'the first snapshot has an empty skills directory');
  assert.ok(!fs.existsSync(path.join(absent.commonsDir, 'skills')),
    'the second has none at all');
  assert.notStrictEqual(empty.version, absent.version);
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

// ---------------------------------------------------------------------------
// Row 27 — neither cleanup path leaks a copy of the commons
// ---------------------------------------------------------------------------

test('a failed copy leaves no dispatch directory behind', () => {
  // mkdtemp mints the directory before cpSync runs, and subscriber.js's guarded
  // removeSnapshot cannot reach it: createSnapshot never returned a snapshot to
  // pass. Three attempts per task at MAX_ATTEMPTS used to mean three full
  // copies, or three empty shells, left in the container's temporary directory.
  const root = makeRoot();
  assert.throws(() => createSnapshot({ source: path.join(root, 'no-such-commons'), root }), /ENOENT/);
  assert.deepStrictEqual(fs.readdirSync(root), [], 'nothing was left in the snapshot root');
});

// ---------------------------------------------------------------------------
// Row 33 — why the commons must hold no symlink
// ---------------------------------------------------------------------------

test('a symlink in the commons survives the copy as a symlink into the mount, and the version does not cover it', () => {
  // `dereference: true` does not reach an entry under the tree: at Node 22 the
  // link is copied as a link, its target rewritten to an absolute path back
  // into the source. So a symlinked tool in the snapshot still resolves through
  // the live /agent-docs mount — the one thing the snapshot exists to stop —
  // and listFiles' `entry.isFile()`, false for a link, keeps it out of the
  // stamp. This test is what a Node upgrade that changes either behaviour
  // trips, so the comment on COMMONS_SOURCE stays true.
  const source = makeCommons({ 'tools/target.js': "'use strict';\nmodule.exports = 1;\n" });
  fs.symlinkSync('target.js', path.join(source, 'tools', 'linked.js'));

  const snapshot = createSnapshot({ source, root: makeRoot() });
  const copied = path.join(snapshot.toolsDir, 'linked.js');

  assert.ok(fs.lstatSync(copied).isSymbolicLink(), 'the copy is still a symlink, not a regular file');
  assert.strictEqual(fs.readlinkSync(copied), path.join(source, 'tools', 'target.js'),
    'and it points back into the source tree, which in the container is the live mount');

  // Changing what the link resolves to changes nothing about the stamp.
  const before = snapshot.version;
  fs.writeFileSync(path.join(source, 'tools', 'target.js'), "'use strict';\nmodule.exports = 2;\n");
  const after = createSnapshot({ source, root: makeRoot() });
  assert.notStrictEqual(after.version, before,
    'the target is itself a regular file in the commons, so its own content is stamped');
  assert.strictEqual(fs.readFileSync(copied, 'utf8'), "'use strict';\nmodule.exports = 2;\n",
    'while the already-taken snapshot now reads the changed file through the link');
});

test('a dangling symlink is invisible to the stamp rather than an error', () => {
  const source = makeCommons();
  fs.symlinkSync(path.join(source, 'tools', 'gone.js'), path.join(source, 'tools', 'dangling.js'));
  const withLink = createSnapshot({ source, root: makeRoot() });

  fs.rmSync(path.join(source, 'tools', 'dangling.js'));
  const without = createSnapshot({ source, root: makeRoot() });

  assert.strictEqual(withLink.version, without.version,
    'the stamp cannot tell a commons with a dangling link from one without it');
  assert.ok(!withLink.executables.includes('tools/dangling.js'));
});

test('pruneSnapshotRoot removes the dispatch directories an earlier process left and reports them', () => {
  const source = makeCommons();
  const root = makeRoot();
  const first = createSnapshot({ source, root });
  const second = createSnapshot({ source, root });
  fs.writeFileSync(path.join(second.stateDir, 'a2a-chain.json'), '{"HW-1":"m-1"}');
  // Something in the root that is not a dispatch directory is not ours to
  // delete.
  fs.writeFileSync(path.join(root, 'unrelated.txt'), 'x');

  const removed = pruneSnapshotRoot(root);

  assert.deepStrictEqual(removed, [path.basename(first.dispatchDir), path.basename(second.dispatchDir)].sort());
  assert.ok(!fs.existsSync(first.dispatchDir));
  assert.ok(!fs.existsSync(second.dispatchDir));
  assert.deepStrictEqual(fs.readdirSync(root), ['unrelated.txt']);
  // The mounted commons it copied from is untouched.
  assert.ok(fs.existsSync(path.join(source, 'tools/gateway-publish.js')));
});

test('pruneSnapshotRoot on a root that does not exist yet is a no-op, not an error', () => {
  const removed = pruneSnapshotRoot(path.join(makeRoot(), 'not-created-yet'));
  assert.deepStrictEqual(removed, []);
});

test('the subscriber sweeps the snapshot root once, before its first consumer starts', () => {
  // The sweep is only safe there: dispatches are serial, so from the first
  // consumer on a directory in the root may be the running session's.
  const subscriber = fs.readFileSync(path.join(__dirname, 'subscriber.js'), 'utf8');
  assert.match(subscriber, /pruneSnapshotRoot \} = require\('\.\/dispatch-snapshot'\)/);
  const call = subscriber.indexOf('pruneSnapshotRoot()');
  const firstConsumer = subscriber.indexOf('await consumer.start()');
  assert.notStrictEqual(call, -1, 'the subscriber must call it');
  assert.ok(call < firstConsumer, 'and call it before the first consumer starts');
  assert.strictEqual(subscriber.split('pruneSnapshotRoot()').length - 1, 1, 'exactly once');
});
