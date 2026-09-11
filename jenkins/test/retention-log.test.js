'use strict';

// Integration tests against a real temp file, not a mock filesystem.

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { appendPruneRecord } = require('../scripts/lib/retention-log');

function tempLogPath() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'jenkins-retention-log-'));
  return path.join(dir, 'prune-history.jsonl');
}

function readLines(logPath) {
  return fs
    .readFileSync(logPath, 'utf8')
    .split('\n')
    .filter(Boolean)
    .map((line) => JSON.parse(line));
}

test('appendPruneRecord: writes one JSON line per call, in append order, each with a timestamp', () => {
  const logPath = tempLogPath();
  try {
    appendPruneRecord({ trigger: 'scheduled-workspace', removed: [], reclaimedBytes: 0 }, logPath);
    appendPruneRecord({ trigger: 'scheduled-docker', reclaimedBytes: 12345 }, logPath);

    const lines = readLines(logPath);
    assert.equal(lines.length, 2);
    assert.equal(lines[0].trigger, 'scheduled-workspace');
    assert.equal(lines[1].trigger, 'scheduled-docker');
    assert.equal(lines[1].reclaimedBytes, 12345);
    assert.equal(typeof lines[1].timestamp, 'string');
  } finally {
    fs.rmSync(path.dirname(logPath), { recursive: true, force: true });
  }
});

test('appendPruneRecord: creates parent directories that do not exist yet', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'jenkins-retention-log-'));
  const nested = path.join(dir, 'a', 'b', 'c', 'prune-history.jsonl');
  try {
    appendPruneRecord({ trigger: 'threshold-sweep' }, nested);
    assert.equal(fs.existsSync(nested), true);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('appendPruneRecord: a write failure is swallowed by default (must not fail the prune job)', () => {
  // Pointing the log at a path that can't be created (a file, not a dir,
  // used as a parent directory segment) reliably fails mkdir.
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'jenkins-retention-log-'));
  const blocker = path.join(dir, 'blocker');
  fs.writeFileSync(blocker, 'not a directory');
  const impossiblePath = path.join(blocker, 'sub', 'prune-history.jsonl');
  try {
    assert.doesNotThrow(() => appendPruneRecord({ trigger: 'x' }, impossiblePath));
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('appendPruneRecord: strict:true propagates the write failure', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'jenkins-retention-log-'));
  const blocker = path.join(dir, 'blocker');
  fs.writeFileSync(blocker, 'not a directory');
  const impossiblePath = path.join(blocker, 'sub', 'prune-history.jsonl');
  try {
    assert.throws(() => appendPruneRecord({ trigger: 'x' }, impossiblePath, { strict: true }));
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
