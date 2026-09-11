'use strict';

// Integration-level tests: these touch a real temp directory standing in
// for the jenkins-data workspace volume, exercising the actual filesystem
// walk/removal mechanism rather than only the decision logic in isolation.

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { discoverWorkspaces, directorySizeBytes, removeWorkspace } = require('../scripts/lib/workspace-fs');

function mkTempRoot() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'jenkins-cache-retention-'));
}

function touch(filePath, { mtime } = {}) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  fs.writeFileSync(filePath, 'x'.repeat(10));
  if (mtime) fs.utimesSync(filePath, mtime, mtime);
}

test('discoverWorkspaces: finds multibranch branch workspaces nested under the job dir', () => {
  const root = mkTempRoot();
  try {
    touch(path.join(root, 'hello-world-pipeline', 'dev', 'index.html'));
    touch(path.join(root, 'hello-world-pipeline', 'PR-3', 'index.html'));

    const found = discoverWorkspaces(root, { multibranchJobNames: ['hello-world-pipeline'] });
    const keys = found.map((w) => `${w.job}/${w.branch}`).sort();
    assert.deepEqual(keys, ['hello-world-pipeline/PR-3', 'hello-world-pipeline/dev']);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('discoverWorkspaces: treats a singleton pipeline job dir as its own workspace (branch: null) even though its checkout has subdirectories', () => {
  const root = mkTempRoot();
  try {
    // release-candidate is NOT in multibranchJobNames, even though its
    // checkout contains a subdirectory (src/) -- this is exactly the case
    // filesystem-shape guessing got wrong: a singleton job's own workspace
    // routinely has subdirectories of its own.
    touch(path.join(root, 'release-candidate', 'src', 'main.js'));

    const found = discoverWorkspaces(root, { multibranchJobNames: ['hello-world-pipeline'] });
    assert.equal(found.length, 1);
    assert.equal(found[0].job, 'release-candidate');
    assert.equal(found[0].branch, null);
    assert.equal(found[0].path, path.join(root, 'release-candidate'));
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('discoverWorkspaces: mtimeMs reflects the newest file in the tree, not just the top dir', () => {
  const root = mkTempRoot();
  try {
    const old = new Date('2026-01-01T00:00:00Z');
    const recent = new Date('2026-09-01T00:00:00Z');
    touch(path.join(root, 'release-candidate', 'a.txt'), { mtime: old });
    touch(path.join(root, 'release-candidate', 'b.txt'), { mtime: recent });

    const found = discoverWorkspaces(root);
    assert.equal(found[0].mtimeMs, recent.getTime());
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('discoverWorkspaces: with no multibranchJobNames given, every top-level dir is treated as a singleton job', () => {
  const root = mkTempRoot();
  try {
    touch(path.join(root, 'hello-world-pipeline', 'dev', 'index.html'));

    const found = discoverWorkspaces(root);
    assert.equal(found.length, 1);
    assert.equal(found[0].job, 'hello-world-pipeline');
    assert.equal(found[0].branch, null);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('discoverWorkspaces: empty root returns no workspaces', () => {
  const root = mkTempRoot();
  try {
    assert.deepEqual(discoverWorkspaces(root), []);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('discoverWorkspaces: missing root returns no workspaces rather than throwing', () => {
  assert.deepEqual(discoverWorkspaces('/does/not/exist/at/all'), []);
});

test('directorySizeBytes: sums file sizes recursively', () => {
  const root = mkTempRoot();
  try {
    fs.mkdirSync(path.join(root, 'sub'), { recursive: true });
    fs.writeFileSync(path.join(root, 'a.txt'), 'x'.repeat(100));
    fs.writeFileSync(path.join(root, 'sub', 'b.txt'), 'x'.repeat(50));
    assert.equal(directorySizeBytes(root), 150);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('removeWorkspace: actually deletes the directory tree', () => {
  const root = mkTempRoot();
  try {
    const target = path.join(root, 'hello-world-pipeline', 'PR-9');
    touch(path.join(target, 'index.html'));
    assert.equal(fs.existsSync(target), true);

    removeWorkspace(target);

    assert.equal(fs.existsSync(target), false);
    // Sibling untouched.
    assert.equal(fs.existsSync(root), true);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('removeWorkspace: a missing path is a no-op, not an error', () => {
  assert.doesNotThrow(() => removeWorkspace('/does/not/exist/at/all'));
});

// End-to-end: discover a realistic tree, run it through the real
// selectWorkspacesToPrune decision, and confirm removeWorkspace only
// deletes what the policy said to prune -- the full pipeline this feature
// is built on, exercised against real directories on real disk.
test('end-to-end: discover -> decide -> remove only prunes what the policy selected, in-progress workspace survives', () => {
  const { selectWorkspacesToPrune } = require('../scripts/lib/retention-policy');
  const root = mkTempRoot();
  try {
    const now = Date.now();
    const oldMtime = new Date(now - 20 * 24 * 60 * 60 * 1000);
    const recentMtime = new Date(now - 1 * 24 * 60 * 60 * 1000);

    touch(path.join(root, 'hello-world-pipeline', 'dev', 'index.html'), { mtime: recentMtime });
    touch(path.join(root, 'hello-world-pipeline', 'PR-1', 'index.html'), { mtime: oldMtime }); // stale, should be pruned
    touch(path.join(root, 'hello-world-pipeline', 'PR-2', 'index.html'), { mtime: oldMtime }); // stale but "building"

    const discovered = discoverWorkspaces(root, { multibranchJobNames: ['hello-world-pipeline'] }).map((w) => ({
      ...w,
      building: w.branch === 'PR-2',
    }));

    const { prune } = selectWorkspacesToPrune(discovered, { now });
    for (const w of prune) removeWorkspace(w.path);

    assert.equal(fs.existsSync(path.join(root, 'hello-world-pipeline', 'dev')), true, 'recent workspace survives');
    assert.equal(fs.existsSync(path.join(root, 'hello-world-pipeline', 'PR-1')), false, 'stale non-building workspace is removed');
    assert.equal(fs.existsSync(path.join(root, 'hello-world-pipeline', 'PR-2')), true, 'stale but in-progress workspace survives');
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});
