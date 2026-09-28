'use strict';

/**
 * Tests for the per-dispatch snapshot (V5.0 Agent Commons REQ-02 and its
 * Amendment 1).
 *
 * These cover the snapshot's own mechanics against real directories: the copy,
 * the executable bits, the content hash, the skills replacement, the session
 * environment and the cleanup — and, for Amendment 1, that the copy reaches the
 * per-role definitions and the two role handbooks, that a change to one of them
 * moves the stamp, that a running snapshot is unaffected by a change to the
 * mount, and that no path the session is given for one of them still names
 * `/agent-docs`. They do not stand in for REQ-02's acceptance, which is about
 * what a session started by the subscriber sees and is proved against a running
 * project container.
 *
 * Run through setup/commons/tools/test.sh, which runs this file alongside the
 * artifact helper's tests under the shared suite lock.
 */

const { test, after } = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const os = require('os');
const path = require('path');

const {
  createSnapshot,
  installSkills,
  sessionEnv,
  rewriteAgentPaths,
  removeSnapshot,
  pruneSnapshotRoot,
} = require('./dispatch-snapshot');

// A stand-in for the whole /agent-docs mount, shaped like the real one: the
// commons (a tools/ directory with one shebang tool and one plain module it
// requires, and a skills/ directory with one skill), the per-role definitions
// under agents/, the two role handbooks — and the entries that stay on the
// mount because no agent reads them as an instruction, which is what lets these
// tests assert the snapshot does *not* carry them.
// Every temporary directory these tests mint, removed when the file is done —
// they are real directories under the OS temp dir, not a mocked filesystem.
const scratch = [];
after(() => {
  for (const dir of scratch) fs.rmSync(dir, { recursive: true, force: true });
});

// The mount-only entries: on the real mount these are the two specifications,
// JenkinsConfig.md, the Jenkinsfile template, graphs/ and subscriber.js.
const MOUNT_ONLY = {
  'SCRUMMASTER_SPEC_v1.md': '# ScrumMaster spec\n\n`/agent-docs/agents/{agent-definition-file}.md`\n',
  'JIRA_SPEC_v1.md': '# Jira spec\n',
  'JenkinsConfig.md': '# Jenkins config log\n',
  'Jenkinsfile.template': 'pipeline { }\n',
  'graphs/migration-status.md': '- see `/agent-docs/agents/devops-agent.md`\n',
  'subscriber.js': "'use strict';\n",
};

// The handbook line agents/devops-agent.md really carries, and the bare mount
// mention its Working Environment list really carries — the first must be
// rewritten onto the snapshot, the second must not (see the tests below).
const DEVOPS_DEFINITION = [
  '# DevOps Agent',
  '',
  '## Working Environment',
  '- Access to shared agent definitions and reference docs at `/agent-docs`',
  '',
  '**Always check the handbook before acting.** The handbook at',
  '`/agent-docs/DEVOPS_HANDBOOK_v1.md` is the source of truth.',
  '',
].join('\n');

function makeMount(files = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'agent-docs-src-'));
  scratch.push(dir);
  const contents = {
    'commons/tools/gateway-publish.js': '#!/usr/bin/env node\nconsole.log("published");\n',
    'commons/tools/envelope.js': "'use strict';\nmodule.exports = {};\n",
    'commons/skills/a2a-submit/SKILL.md': '---\nname: a2a-submit\n---\nbody\n',
    'agents/devops-agent.md': DEVOPS_DEFINITION,
    'agents/backend-agent.md': '# Backend Agent\n\n- `/agent-docs` — reference docs (read-only)\n',
    'DEVOPS_HANDBOOK_v1.md': '# DevOps handbook\n\nHow this system is built.\n',
    'DESKTOP_HANDBOOK_v1.md': '# Desktop handbook\n',
    ...MOUNT_ONLY,
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
  const source = makeMount();
  const root = makeRoot();
  const snapshot = createSnapshot({ source, root });

  assert.strictEqual(
    fs.readFileSync(path.join(snapshot.toolsDir, 'gateway-publish.js'), 'utf8'),
    fs.readFileSync(path.join(source, 'commons/tools/gateway-publish.js'), 'utf8')
  );
  assert.ok(fs.existsSync(path.join(snapshot.commonsDir, 'skills/a2a-submit/SKILL.md')));
  assert.strictEqual(snapshot.toolsDir, path.join(snapshot.commonsDir, 'tools'));
});

test('the snapshot directory is outside the project working tree', () => {
  const source = makeMount();
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
  const source = makeMount();
  const snapshot = createSnapshot({ source, root: makeRoot() });

  const toolMode = fs.statSync(path.join(snapshot.toolsDir, 'gateway-publish.js')).mode & 0o777;
  const moduleMode = fs.statSync(path.join(snapshot.toolsDir, 'envelope.js')).mode & 0o777;
  assert.ok(toolMode & 0o111, `expected executable, got ${toolMode.toString(8)}`);
  assert.strictEqual(moduleMode & 0o111, 0, `expected non-executable, got ${moduleMode.toString(8)}`);
  assert.deepStrictEqual(snapshot.executables, ['tools/gateway-publish.js']);
});

test('the version is a sha256 hex digest and is equal for two snapshots of the same commons', () => {
  const source = makeMount();
  const first = createSnapshot({ source, root: makeRoot() });
  const second = createSnapshot({ source, root: makeRoot() });

  assert.match(first.version, /^[0-9a-f]{64}$/);
  assert.strictEqual(first.version, second.version);
});

test('changing a file in the commons changes the next snapshot version', () => {
  const source = makeMount();
  const before = createSnapshot({ source, root: makeRoot() });

  fs.writeFileSync(path.join(source, 'commons/skills/a2a-submit/SKILL.md'), '---\nname: a2a-submit\n---\nchanged\n');
  const after = createSnapshot({ source, root: makeRoot() });

  assert.notStrictEqual(after.version, before.version);
});

test('the version covers file paths, not only contents', () => {
  const a = makeMount({ 'commons/tools/one.js': "'use strict';\n" });
  const b = makeMount({ 'commons/tools/two.js': "'use strict';\n" });

  const first = createSnapshot({ source: a, root: makeRoot() });
  const second = createSnapshot({ source: b, root: makeRoot() });

  assert.notStrictEqual(first.version, second.version);
});

test('an empty directory and an absent one stamp differently', () => {
  // installSkills' own existsSync branch behaves differently for the two, so a
  // stamp that could not tell them apart would call two sessions that ran
  // differently the same commons.
  const withEmpty = makeMount();
  fs.rmSync(path.join(withEmpty, 'commons/skills/a2a-submit'), { recursive: true });
  const without = makeMount();
  fs.rmSync(path.join(without, 'commons/skills'), { recursive: true });

  const empty = createSnapshot({ source: withEmpty, root: makeRoot() });
  const absent = createSnapshot({ source: without, root: makeRoot() });

  assert.deepStrictEqual(fs.readdirSync(path.join(empty.commonsDir, 'skills')), [],
    'the first snapshot has an empty skills directory');
  assert.ok(!fs.existsSync(path.join(absent.commonsDir, 'skills')),
    'the second has none at all');
  assert.notStrictEqual(empty.version, absent.version);
});

test('the version ignores file modes, so a source tree without executable bits still stamps the same', () => {
  const source = makeMount();
  const bare = createSnapshot({ source, root: makeRoot() });

  fs.chmodSync(path.join(source, 'commons/tools/gateway-publish.js'), 0o755);
  fs.chmodSync(path.join(source, 'commons/tools/envelope.js'), 0o600);
  const remoded = createSnapshot({ source, root: makeRoot() });

  assert.strictEqual(remoded.version, bare.version);
});

test('a running snapshot is unaffected by a later change to the commons', () => {
  const source = makeMount();
  const snapshot = createSnapshot({ source, root: makeRoot() });
  const asDispatched = fs.readFileSync(path.join(snapshot.toolsDir, 'gateway-publish.js'), 'utf8');
  const stampedVersion = snapshot.version;

  fs.writeFileSync(path.join(source, 'commons/tools/gateway-publish.js'), '#!/usr/bin/env node\nconsole.log("changed");\n');

  assert.strictEqual(fs.readFileSync(path.join(snapshot.toolsDir, 'gateway-publish.js'), 'utf8'), asDispatched);
  assert.strictEqual(snapshot.version, stampedVersion);
});

test('two successive dispatches get different directories and neither reads the other', () => {
  const source = makeMount();
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
  const source = makeMount();
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
  const source = makeMount();
  const snapshot = createSnapshot({ source, root: makeRoot() });
  const env = sessionEnv(snapshot, '/usr/local/bin:/usr/bin');

  assert.strictEqual(env.PATH, `${snapshot.toolsDir}:/usr/local/bin:/usr/bin`);
  assert.strictEqual(env.PATH.split(':')[0], snapshot.toolsDir);
  assert.strictEqual(env.AIGANG_COMMONS_DIR, snapshot.commonsDir);
  assert.strictEqual(env.AIGANG_COMMONS_VERSION, snapshot.version);
});

test('installing skills replaces whatever was in the skills directory', () => {
  const source = makeMount();
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
  const source = makeMount();
  const snapshot = createSnapshot({ source, root: makeRoot() });
  const skillsHome = path.join(makeRoot(), 'fresh-home', '.claude', 'skills');

  installSkills(snapshot, skillsHome);

  assert.deepStrictEqual(fs.readdirSync(skillsHome), ['a2a-submit']);
});

test('a commons with no skills directory leaves an empty skills directory', () => {
  const source = makeMount();
  fs.rmSync(path.join(source, 'commons/skills'), { recursive: true });
  const snapshot = createSnapshot({ source, root: makeRoot() });
  const skillsHome = path.join(makeRoot(), '.claude', 'skills');
  fs.mkdirSync(path.join(skillsHome, 'retired-skill'), { recursive: true });

  installSkills(snapshot, skillsHome);

  assert.deepStrictEqual(fs.readdirSync(skillsHome), []);
});

test('removing the snapshot removes the snapshot and the state beside it', () => {
  const source = makeMount();
  const snapshot = createSnapshot({ source, root: makeRoot() });
  fs.writeFileSync(path.join(snapshot.stateDir, 'chain.json'), '{}');

  removeSnapshot(snapshot);

  assert.ok(!fs.existsSync(snapshot.dispatchDir));
  assert.ok(!fs.existsSync(snapshot.commonsDir));
  assert.ok(!fs.existsSync(snapshot.stateDir));
  // Everything the dispatch created, which now includes the definitions and
  // the handbooks it carried.
  assert.ok(!fs.existsSync(path.join(snapshot.dispatchDir, 'agents/devops-agent.md')));
  assert.ok(!fs.existsSync(path.join(snapshot.dispatchDir, 'DEVOPS_HANDBOOK_v1.md')));
  assert.ok(!fs.existsSync(path.join(snapshot.dispatchDir, 'DESKTOP_HANDBOOK_v1.md')));
  // The mount it was copied from is untouched.
  assert.ok(fs.existsSync(path.join(source, 'commons/tools/gateway-publish.js')));
  assert.ok(fs.existsSync(path.join(source, 'agents/devops-agent.md')));
  assert.ok(fs.existsSync(path.join(source, 'DEVOPS_HANDBOOK_v1.md')));
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
  const source = makeMount({ 'commons/tools/target.js': "'use strict';\nmodule.exports = 1;\n" });
  fs.symlinkSync('target.js', path.join(source, 'commons', 'tools', 'linked.js'));

  const snapshot = createSnapshot({ source, root: makeRoot() });
  const copied = path.join(snapshot.toolsDir, 'linked.js');

  assert.ok(fs.lstatSync(copied).isSymbolicLink(), 'the copy is still a symlink, not a regular file');
  assert.strictEqual(fs.readlinkSync(copied), path.join(source, 'commons', 'tools', 'target.js'),
    'and it points back into the source tree, which in the container is the live mount');

  // Changing what the link resolves to changes nothing about the stamp.
  const before = snapshot.version;
  fs.writeFileSync(path.join(source, 'commons', 'tools', 'target.js'), "'use strict';\nmodule.exports = 2;\n");
  const after = createSnapshot({ source, root: makeRoot() });
  assert.notStrictEqual(after.version, before,
    'the target is itself a regular file in the commons, so its own content is stamped');
  assert.strictEqual(fs.readFileSync(copied, 'utf8'), "'use strict';\nmodule.exports = 2;\n",
    'while the already-taken snapshot now reads the changed file through the link');
});

test('a dangling symlink is invisible to the stamp rather than an error', () => {
  const source = makeMount();
  fs.symlinkSync(path.join(source, 'commons', 'tools', 'gone.js'), path.join(source, 'commons', 'tools', 'dangling.js'));
  const withLink = createSnapshot({ source, root: makeRoot() });

  fs.rmSync(path.join(source, 'commons', 'tools', 'dangling.js'));
  const without = createSnapshot({ source, root: makeRoot() });

  assert.strictEqual(withLink.version, without.version,
    'the stamp cannot tell a commons with a dangling link from one without it');
  assert.ok(!withLink.executables.includes('tools/dangling.js'));
});

test('pruneSnapshotRoot removes the dispatch directories an earlier process left and reports them', () => {
  const source = makeMount();
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
  assert.ok(fs.existsSync(path.join(source, 'commons/tools/gateway-publish.js')));
});

test('pruneSnapshotRoot on a root that does not exist yet is a no-op, not an error', () => {
  const removed = pruneSnapshotRoot(path.join(makeRoot(), 'not-created-yet'));
  assert.deepStrictEqual(removed, []);
});

test('the subscriber sweeps the snapshot root once, before its first consumer starts', () => {
  // The sweep is only safe there: dispatches are serial, so from the first
  // consumer on a directory in the root may be the running session's.
  const subscriber = fs.readFileSync(path.join(__dirname, 'subscriber.js'), 'utf8');
  assert.match(subscriber, /pruneSnapshotRoot,\n\} = require\('\.\/dispatch-snapshot'\);/);
  const call = subscriber.indexOf('pruneSnapshotRoot()');
  const firstConsumer = subscriber.indexOf('await consumer.start()');
  assert.notStrictEqual(call, -1, 'the subscriber must call it');
  assert.ok(call < firstConsumer, 'and call it before the first consumer starts');
  assert.strictEqual(subscriber.split('pruneSnapshotRoot()').length - 1, 1, 'exactly once');
});

// ---------------------------------------------------------------------------
// Amendment 1 (audit row 26) — the snapshot carries everything an agent reads
// during its session, and no path it is given for one of those files names the
// live mount.
//
// REQ-02's acceptance, in its own words: "with a session running, changing
// `/agent-docs/agents/<role>.md` and `/agent-docs/DEVOPS_HANDBOOK_v1.md` on the
// host leaves what that session reads unchanged, the next dispatch reads the
// change and logs a different hash, and no path the session is given for either
// file names `/agent-docs`."
// ---------------------------------------------------------------------------

test('the snapshot carries the role definitions and both handbooks beside the commons', () => {
  const source = makeMount();
  const snapshot = createSnapshot({ source, root: makeRoot() });

  for (const rel of ['agents/devops-agent.md', 'agents/backend-agent.md',
    'DEVOPS_HANDBOOK_v1.md', 'DESKTOP_HANDBOOK_v1.md']) {
    assert.ok(fs.existsSync(path.join(snapshot.dispatchDir, rel)), `${rel} is in the snapshot`);
  }
  // A handbook is carried byte for byte; a definition differs only by the paths
  // rewritten onto this snapshot, which the tests below cover.
  assert.strictEqual(
    fs.readFileSync(path.join(snapshot.dispatchDir, 'DEVOPS_HANDBOOK_v1.md'), 'utf8'),
    fs.readFileSync(path.join(source, 'DEVOPS_HANDBOOK_v1.md'), 'utf8')
  );
  // Each keeps the name it has under the mount, which is what makes the rewrite
  // a single prefix rule.
  assert.deepStrictEqual(snapshot.entries,
    ['commons', 'agents', 'DEVOPS_HANDBOOK_v1.md', 'DESKTOP_HANDBOOK_v1.md']);
});

test('the snapshot carries nothing else from the mount: the specifications, graphs/ and subscriber.js stay on it', () => {
  const source = makeMount();
  const snapshot = createSnapshot({ source, root: makeRoot() });

  assert.deepStrictEqual(fs.readdirSync(snapshot.dispatchDir).sort(),
    ['DESKTOP_HANDBOOK_v1.md', 'DEVOPS_HANDBOOK_v1.md', 'agents', 'commons', 'state']);
  for (const rel of Object.keys(MOUNT_ONLY)) {
    assert.ok(!fs.existsSync(path.join(snapshot.dispatchDir, rel)),
      `${rel} is read by the platform or a person, not by an agent as an instruction`);
    assert.ok(fs.existsSync(path.join(source, rel)), `${rel} is still on the mount`);
  }
});

test('changing a role definition changes the next snapshot version', () => {
  const source = makeMount();
  const before = createSnapshot({ source, root: makeRoot() });

  fs.writeFileSync(path.join(source, 'agents/devops-agent.md'), DEVOPS_DEFINITION + '\nOne more rule.\n');
  const after = createSnapshot({ source, root: makeRoot() });

  assert.notStrictEqual(after.version, before.version);
  assert.match(fs.readFileSync(path.join(after.dispatchDir, 'agents/devops-agent.md'), 'utf8'),
    /One more rule\./, 'and the next dispatch reads the change');
});

test('changing a handbook changes the next snapshot version', () => {
  const source = makeMount();
  const before = createSnapshot({ source, root: makeRoot() });

  fs.writeFileSync(path.join(source, 'DEVOPS_HANDBOOK_v1.md'), '# DevOps handbook\n\nChanged.\n');
  const after = createSnapshot({ source, root: makeRoot() });

  assert.notStrictEqual(after.version, before.version);
  assert.match(fs.readFileSync(path.join(after.dispatchDir, 'DEVOPS_HANDBOOK_v1.md'), 'utf8'), /Changed\./);
});

test('a running snapshot keeps the definition and the handbook it was dispatched with', () => {
  // The mid-session case: the host changes both files while the session runs.
  const source = makeMount();
  const snapshot = createSnapshot({ source, root: makeRoot() });
  const definition = fs.readFileSync(path.join(snapshot.dispatchDir, 'agents/devops-agent.md'), 'utf8');
  const handbook = fs.readFileSync(path.join(snapshot.dispatchDir, 'DEVOPS_HANDBOOK_v1.md'), 'utf8');
  const stamped = snapshot.version;

  fs.writeFileSync(path.join(source, 'agents/devops-agent.md'), '# DevOps Agent\n\nReplaced mid-session.\n');
  fs.writeFileSync(path.join(source, 'DEVOPS_HANDBOOK_v1.md'), '# DevOps handbook\n\nReplaced mid-session.\n');

  assert.strictEqual(fs.readFileSync(path.join(snapshot.dispatchDir, 'agents/devops-agent.md'), 'utf8'), definition);
  assert.strictEqual(fs.readFileSync(path.join(snapshot.dispatchDir, 'DEVOPS_HANDBOOK_v1.md'), 'utf8'), handbook);
  assert.strictEqual(snapshot.version, stamped);
  assert.doesNotMatch(definition, /Replaced mid-session/);
  assert.doesNotMatch(handbook, /Replaced mid-session/);
});

test("every definitionPath ScrumMaster prints is rewritten onto the session's own snapshot", () => {
  // The prompt's first lines are the role definition's path, taken verbatim from
  // ScrumMaster's own config (`definitionPath`, printed by prompt.js). This
  // feature does not change that config, so the subscriber rewrites the prompt
  // it received — and this reads the real config so a change to any of the five
  // values, or a sixth role, trips here.
  const config = JSON.parse(fs.readFileSync(
    path.join(__dirname, '..', 'services', 'scrummaster', 'config', 'agents.json'), 'utf8'));
  const paths = config.agents.map(a => a.definitionPath);
  assert.ok(paths.length >= 5, `expected every role, got ${paths.length}`);

  const source = makeMount();
  const snapshot = createSnapshot({ source, root: makeRoot() });

  for (const definitionPath of paths) {
    const prompt = [
      '## ROLE',
      'You are the DevOps Agent. Read your full agent definition before taking any action:',
      definitionPath,
      '',
      '## TICKET CONTEXT',
      'Ticket: HW-1 — a ticket',
    ].join('\n');
    const rewritten = rewriteAgentPaths(prompt, snapshot);

    assert.ok(!rewritten.includes('/agent-docs'),
      `no path the session is given names the mount: ${rewritten}`);
    assert.ok(rewritten.includes(path.join(snapshot.dispatchDir, definitionPath.slice('/agent-docs/'.length))),
      `${definitionPath} resolves inside this dispatch's snapshot`);
    // Everything else in the prompt is untouched.
    assert.ok(rewritten.includes('Ticket: HW-1 — a ticket'));
  }
});

test('the handbook path a role definition makes mandatory resolves inside the snapshot, and the file is there', () => {
  const source = makeMount();
  const snapshot = createSnapshot({ source, root: makeRoot() });

  const definition = fs.readFileSync(path.join(snapshot.dispatchDir, 'agents/devops-agent.md'), 'utf8');
  const expected = path.join(snapshot.dispatchDir, 'DEVOPS_HANDBOOK_v1.md');
  assert.ok(definition.includes(expected), definition);
  assert.ok(!definition.includes('/agent-docs/DEVOPS_HANDBOOK_v1.md'));
  assert.ok(fs.existsSync(expected), 'and the path it now names is a real file');
  assert.deepStrictEqual(snapshot.rewritten, ['agents/devops-agent.md']);
});

test('a bare /agent-docs mention stays: it names the mount as a directory, which is still there', () => {
  // The deliberate choice. The Working Environment lists in three role
  // definitions name `/agent-docs` with no file after it. The mount still
  // exists, is still read-only, and still holds the specifications, graphs/ and
  // the Jenkinsfile template; rewriting the bare mention onto the snapshot,
  // which holds none of those and is writable, would make a true sentence false.
  // No file is read through it, so it is not a path REQ-02's clause covers.
  const source = makeMount();
  const snapshot = createSnapshot({ source, root: makeRoot() });

  const devops = fs.readFileSync(path.join(snapshot.dispatchDir, 'agents/devops-agent.md'), 'utf8');
  const backend = fs.readFileSync(path.join(snapshot.dispatchDir, 'agents/backend-agent.md'), 'utf8');
  assert.ok(devops.includes('reference docs at `/agent-docs`'), devops);
  assert.strictEqual(backend, fs.readFileSync(path.join(source, 'agents/backend-agent.md'), 'utf8'),
    'a definition whose only mention is the bare mount is copied byte for byte');
});

test('a path under an entry that stays on the mount is not rewritten', () => {
  const source = makeMount();
  const snapshot = createSnapshot({ source, root: makeRoot() });

  const kept = [
    '/agent-docs/SCRUMMASTER_SPEC_v1.md',
    '/agent-docs/JIRA_SPEC_v1.md',
    '/agent-docs/JenkinsConfig.md',
    '/agent-docs/Jenkinsfile.template',
    '/agent-docs/graphs/migration-status.md',
    '/agent-docs/subscriber.js',
  ].join('\n');
  assert.strictEqual(rewriteAgentPaths(kept, snapshot), kept);
});

test('two dispatches of one mount stamp the same version, though their rewritten documents differ', () => {
  // The stamp is taken before the rewrite, and has to be: what the rewrite
  // substitutes in is the dispatch's own mkdtemp directory, different every
  // time, so hashing after it would give two sessions that ran the same commons
  // two different versions — the one thing the stamp exists to rule out.
  const source = makeMount();
  const first = createSnapshot({ source, root: makeRoot() });
  const second = createSnapshot({ source, root: makeRoot() });

  assert.strictEqual(first.version, second.version);
  assert.notStrictEqual(
    fs.readFileSync(path.join(first.dispatchDir, 'agents/devops-agent.md'), 'utf8'),
    fs.readFileSync(path.join(second.dispatchDir, 'agents/devops-agent.md'), 'utf8')
  );
});

test('a mount missing a handbook fails the dispatch and leaves nothing behind', () => {
  const source = makeMount();
  fs.rmSync(path.join(source, 'DESKTOP_HANDBOOK_v1.md'));
  const root = makeRoot();

  assert.throws(() => createSnapshot({ source, root }), /ENOENT.*DESKTOP_HANDBOOK_v1\.md/);
  assert.deepStrictEqual(fs.readdirSync(root), [], 'nothing was left in the snapshot root');
});

// ---------------------------------------------------------------------------
// The same properties against the real setup/ tree, which is what the mount is
// ---------------------------------------------------------------------------

test('the real setup/ tree snapshots exactly the four entries, and the state derivation still holds', () => {
  const snapshot = createSnapshot({ source: __dirname, root: makeRoot() });

  assert.deepStrictEqual(fs.readdirSync(snapshot.dispatchDir).sort(),
    ['DESKTOP_HANDBOOK_v1.md', 'DEVOPS_HANDBOOK_v1.md', 'agents', 'commons', 'state']);
  assert.deepStrictEqual(fs.readdirSync(path.join(snapshot.dispatchDir, 'agents')).sort(),
    ['backend-agent.md', 'devops-agent.md', 'frontend-agent.md', 'refinement-agent.md']);
  // What stays on the mount is still only on the mount. This list plus the
  // four snapshotted entries is every top-level entry of `setup/`, so a new one
  // has to be classified rather than land unnoticed on either side (V5.0 audit
  // row 89).
  const keptOnTheMount = ['SCRUMMASTER_SPEC_v1.md', 'JIRA_SPEC_v1.md', 'JenkinsConfig.md',
    'Jenkinsfile.template', 'graphs', 'subscriber.js', 'dispatch-snapshot.js',
    'dispatch-snapshot.test.js'];
  for (const rel of keptOnTheMount) {
    assert.ok(fs.existsSync(path.join(__dirname, rel)), `${rel} is on the mount`);
    assert.ok(!fs.existsSync(path.join(snapshot.dispatchDir, rel)), `${rel} is not snapshotted`);
  }
  assert.deepStrictEqual(
    fs.readdirSync(__dirname).sort(),
    [...keptOnTheMount, ...snapshot.entries].sort(),
    'setup/ has an entry that is neither snapshotted nor enumerated as kept on the mount'
  );

  // a2a-submit.js derives its per-session state directory from
  // AIGANG_COMMONS_DIR's parent, and the definitions and handbooks sitting in
  // that same parent must not have moved it.
  const env = sessionEnv(snapshot, '/usr/bin');
  assert.strictEqual(path.join(path.dirname(env.AIGANG_COMMONS_DIR), 'state'), snapshot.stateDir);
  assert.ok(fs.statSync(snapshot.stateDir).isDirectory());
});

test('no snapshotted markdown in the real setup/ tree still names a snapshotted entry under /agent-docs', () => {
  // The gate for a mention added later: a new role definition, a new handbook
  // section or a SKILL.md that hardcodes the mount fails here.
  const snapshot = createSnapshot({ source: __dirname, root: makeRoot() });
  const prefixes = snapshot.entries.map(entry => `/agent-docs/${entry}`);

  const offenders = [];
  const walk = (dir, prefix = '') => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const rel = prefix ? `${prefix}/${entry.name}` : entry.name;
      if (rel === 'state') continue;
      if (entry.isDirectory()) walk(path.join(dir, entry.name), rel);
      else if (entry.isFile() && rel.endsWith('.md')) {
        const body = fs.readFileSync(path.join(dir, entry.name), 'utf8');
        if (prefixes.some(p => body.includes(p))) offenders.push(rel);
      }
    }
  };
  walk(snapshot.dispatchDir);

  assert.deepStrictEqual(offenders, []);
  // And the rewrite really did something, so this is not vacuous.
  assert.deepStrictEqual(snapshot.rewritten,
    ['DESKTOP_HANDBOOK_v1.md', 'DEVOPS_HANDBOOK_v1.md', 'agents/devops-agent.md']);
});

// Every `*_HANDBOOK_v1.md` mention in a snapshotted markdown file, as the
// whole path-shaped token it sits in. A mention the rewrite carried is
// absolute — `<dispatchDir>/DEVOPS_HANDBOOK_v1.md` — so anything that does not
// start with `/` is a bare filename, which is the case the prefix test above
// structurally cannot see: it searches for `/agent-docs/<entry>`, and a bare
// name carries no prefix to find (V5.0 audit row 83).
function bareHandbookMentions(dispatchDir) {
  const bare = [];
  const walk = (dir, prefix = '') => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const rel = prefix ? `${prefix}/${entry.name}` : entry.name;
      if (rel === 'state') continue;
      if (entry.isDirectory()) walk(path.join(dir, entry.name), rel);
      else if (entry.isFile() && rel.endsWith('.md')) {
        const body = fs.readFileSync(path.join(dir, entry.name), 'utf8');
        for (const token of body.match(/[A-Za-z0-9._/-]*_HANDBOOK_v1\.md/g) || []) {
          if (!token.startsWith('/')) bare.push(`${rel}: ${token}`);
        }
      }
    }
  };
  walk(dispatchDir);
  return bare.sort();
}

test('no snapshotted markdown names a snapshotted handbook by bare filename', () => {
  // The handbooks sit at the dispatch directory's root while a definition
  // naming one sits in `agents/`, so a bare name resolves from neither. Only an
  // absolute `/agent-docs/...` path is a path the rewrite can carry into the
  // snapshot, and the prefix test above cannot catch a bare one.
  const snapshot = createSnapshot({ source: __dirname, root: makeRoot() });
  assert.deepStrictEqual(bareHandbookMentions(snapshot.dispatchDir), []);
});

test('a bare handbook mention added to a snapshotted document is caught', () => {
  // Not vacuous: the same check over a mount carrying exactly the mistake row
  // 83 found — a definition and a handbook each naming the other handbook by
  // bare filename — names both files.
  const source = makeMount();
  fs.writeFileSync(path.join(source, 'agents/devops-agent.md'),
    'See `DESKTOP_HANDBOOK_v1.md` for desktop builds.\n');
  fs.writeFileSync(path.join(source, 'DESKTOP_HANDBOOK_v1.md'),
    '# Desktop handbook\n\nIt supplements `DEVOPS_HANDBOOK_v1.md`.\n');
  const snapshot = createSnapshot({ source, root: makeRoot() });

  assert.deepStrictEqual(bareHandbookMentions(snapshot.dispatchDir), [
    'DESKTOP_HANDBOOK_v1.md: DEVOPS_HANDBOOK_v1.md',
    'agents/devops-agent.md: DESKTOP_HANDBOOK_v1.md',
  ]);
});

test('the only snapshotted non-markdown files naming a mount path are three comments', () => {
  // Not rewritten on purpose: a comment is not a path an agent is given, and
  // gateway-publish.js's usage lines document Jenkins' own invocation, which
  // REQ-04 keeps on the mount. This list is the gate — a tool that hardcodes the
  // mount in code lands here and has to be dealt with rather than slipping in.
  const snapshot = createSnapshot({ source: __dirname, root: makeRoot() });
  const prefixes = snapshot.entries.map(entry => `/agent-docs/${entry}`);

  const found = [];
  const walk = (dir, prefix = '') => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const rel = prefix ? `${prefix}/${entry.name}` : entry.name;
      if (rel === 'state') continue;
      if (entry.isDirectory()) walk(path.join(dir, entry.name), rel);
      else if (entry.isFile() && !rel.endsWith('.md')) {
        const body = fs.readFileSync(path.join(dir, entry.name), 'utf8');
        if (prefixes.some(p => body.includes(p))) found.push(rel);
      }
    }
  };
  walk(snapshot.dispatchDir);

  assert.deepStrictEqual(found.sort(), [
    'commons/tools/a2a-schema.js',
    'commons/tools/envelope.js',
    'commons/tools/gateway-publish.js',
  ]);
});

test('the subscriber rewrites the prompt it received before writing it to the session', () => {
  // The real enforcement point is a dispatch into a running container, which is
  // where REQ-02's acceptance is proved. This is the cheap structural half: the
  // prompt that reaches the session is the rewritten one, and the rewrite
  // happens after the snapshot it rewrites onto exists.
  const subscriber = fs.readFileSync(path.join(__dirname, 'subscriber.js'), 'utf8');
  assert.match(subscriber, /rewriteAgentPaths,\n/);
  const created = subscriber.indexOf('snapshot = createSnapshot()');
  const rewritten = subscriber.indexOf('const sessionPrompt = rewriteAgentPaths(prompt, snapshot);');
  const written = subscriber.indexOf('child.stdin.write(sessionPrompt);');
  assert.notStrictEqual(rewritten, -1, 'the subscriber rewrites the prompt');
  assert.notStrictEqual(written, -1, 'and writes the rewritten prompt, not the one it received');
  assert.ok(created < rewritten && rewritten < written);
  assert.strictEqual(subscriber.split('child.stdin.write(').length - 1, 1, 'and writes stdin once');
});
