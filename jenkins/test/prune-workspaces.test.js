'use strict';

// Integration test for the full prune-workspaces orchestration: a real
// temp directory stands in for the jenkins-data workspace volume, and a
// fake fetchFn stands in for the Jenkins REST API (there's no live Jenkins
// controller to query in a unit-test sandbox). Exercises discover -> decide
// -> delete -> log end to end, not just the decision logic.

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

function mkTempWorkspaceRoot() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'jenkins-prune-ws-'));
}

function touch(filePath, { mtime } = {}) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  fs.writeFileSync(filePath, 'x'.repeat(10));
  if (mtime) fs.utimesSync(filePath, mtime, mtime);
}

async function withTempLog(fn) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'jenkins-prune-ws-log-'));
  const logPath = path.join(dir, 'prune-history.jsonl');
  process.env.RETENTION_LOG_PATH = logPath;
  delete require.cache[require.resolve('../scripts/lib/retention-log')];
  delete require.cache[require.resolve('../scripts/prune-workspaces')];
  try {
    return await fn(logPath);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
    delete process.env.RETENTION_LOG_PATH;
  }
}

// Reads the last appended record straight off disk -- retention-log.js only
// exposes the write side (appendPruneRecord); these tests verify what it
// wrote by reading the .jsonl file directly rather than through any
// library read-back helper.
function readLastRecord(logPath) {
  const lines = fs.readFileSync(logPath, 'utf8').split('\n').filter(Boolean);
  return JSON.parse(lines[lines.length - 1]);
}

test('prune-workspaces main(): removes stale non-building workspaces, keeps recent and building ones, logs the run', async () => {
  const root = mkTempWorkspaceRoot();
  await withTempLog(async (logPath) => {
    try {
      const now = Date.now();
      const old = new Date(now - 20 * 24 * 60 * 60 * 1000);
      const recent = new Date(now - 1 * 24 * 60 * 60 * 1000);

      touch(path.join(root, 'hello-world-pipeline', 'dev', 'index.html'), { mtime: recent });
      touch(path.join(root, 'hello-world-pipeline', 'PR-1', 'index.html'), { mtime: old }); // stale, not building
      touch(path.join(root, 'hello-world-pipeline', 'PR-2', 'index.html'), { mtime: old }); // stale, building

      const fakeFetch = async () => ({
        ok: true,
        json: async () => ({
          jobs: [
            {
              name: 'hello-world-pipeline',
              jobs: [
                { name: 'dev', lastBuild: { building: false } },
                { name: 'PR-1', lastBuild: { building: false } },
                { name: 'PR-2', lastBuild: { building: true } },
              ],
            },
          ],
        }),
      });

      const { main } = require('../scripts/prune-workspaces');
      await main([], { workspaceRoot: root, fetchFn: fakeFetch });

      assert.equal(fs.existsSync(path.join(root, 'hello-world-pipeline', 'dev')), true);
      assert.equal(fs.existsSync(path.join(root, 'hello-world-pipeline', 'PR-1')), false);
      assert.equal(fs.existsSync(path.join(root, 'hello-world-pipeline', 'PR-2')), true, 'in-progress build workspace must survive');

      const record = readLastRecord(logPath);
      assert.equal(record.trigger, 'scheduled-workspace');
      assert.equal(record.removed.length, 1);
      assert.equal(record.removed[0].branch, 'PR-1');
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  });
});

test('prune-workspaces main(): --dry-run reports what would be removed without deleting anything', async () => {
  const root = mkTempWorkspaceRoot();
  await withTempLog(async () => {
    try {
      const old = new Date(Date.now() - 20 * 24 * 60 * 60 * 1000);
      touch(path.join(root, 'hello-world-pipeline', 'PR-1', 'index.html'), { mtime: old });

      const fakeFetch = async () => ({ ok: true, json: async () => ({ jobs: [] }) });
      const { main } = require('../scripts/prune-workspaces');
      await main(['--dry-run'], { workspaceRoot: root, fetchFn: fakeFetch });

      assert.equal(fs.existsSync(path.join(root, 'hello-world-pipeline', 'PR-1')), true, '--dry-run must not delete');
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  });
});

test('prune-workspaces main(): a Jenkins API failure skips the run entirely rather than guessing what is safe', async () => {
  const root = mkTempWorkspaceRoot();
  await withTempLog(async (logPath) => {
    try {
      const old = new Date(Date.now() - 20 * 24 * 60 * 60 * 1000);
      touch(path.join(root, 'hello-world-pipeline', 'PR-1', 'index.html'), { mtime: old });

      const failingFetch = async () => ({ ok: false, status: 500, statusText: 'Internal Server Error' });
      const { main } = require('../scripts/prune-workspaces');
      await main([], { workspaceRoot: root, fetchFn: failingFetch });

      assert.equal(fs.existsSync(path.join(root, 'hello-world-pipeline', 'PR-1')), true, 'nothing must be removed when build state is unknown');

      const record = readLastRecord(logPath);
      assert.equal(record.skipped, true);
      assert.equal(record.reason, 'jenkins-api-unreachable');
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  });
});

test('prune-workspaces main(): a workspace root with nothing prunable removes nothing and still logs a clean run', async () => {
  const root = mkTempWorkspaceRoot();
  await withTempLog(async (logPath) => {
    try {
      touch(path.join(root, 'hello-world-pipeline', 'dev', 'index.html'));

      const fakeFetch = async () => ({ ok: true, json: async () => ({ jobs: [] }) });
      const { main } = require('../scripts/prune-workspaces');
      await main([], { workspaceRoot: root, fetchFn: fakeFetch });

      const record = readLastRecord(logPath);
      assert.equal(record.removed.length, 0);
      assert.equal(record.reclaimedBytes, 0);
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  });
});
