'use strict';

// Integration-level test: exercises prune-docker-cache's real orchestration
// (building the argv, invoking "docker", parsing its output, writing the
// retention log) against a fake docker CLI implementation instead of a real
// Docker daemon -- there's no Docker daemon available in a unit-test
// sandbox, so this is the documented substitute the task instructions call
// for ("a mocked/fake Docker CLI").

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

function withTempLog(fn) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'jenkins-prune-docker-'));
  const logPath = path.join(dir, 'prune-history.jsonl');
  process.env.RETENTION_LOG_PATH = logPath;
  delete require.cache[require.resolve('../scripts/lib/retention-log')];
  delete require.cache[require.resolve('../scripts/prune-docker-cache')];
  try {
    return fn(logPath);
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

test('prune-docker-cache run(): invokes the exact command buildDockerPruneCommand produces', () => {
  withTempLog((logPath) => {
    const { run } = require('../scripts/prune-docker-cache');
    let calledCmd, calledArgs;
    const fakeExecFn = (cmd, args) => {
      calledCmd = cmd;
      calledArgs = args;
      return 'Deleted Images:\ndeleted: sha256:abc\n\nTotal reclaimed space: 1.2GB\n';
    };

    const result = run({ execFn: fakeExecFn, trigger: 'scheduled-docker' });

    assert.equal(calledCmd, 'docker');
    assert.deepEqual(calledArgs, ['system', 'prune', '-f', '--filter', 'until=72h']);
    assert.equal(result.reclaimedBytes, 1_200_000_000);

    const record = readLastRecord(logPath);
    assert.equal(record.trigger, 'scheduled-docker');
    assert.equal(record.reclaimedBytes, 1_200_000_000);
  });
});

test('prune-docker-cache run(): a fake CLI reporting nothing to reclaim is handled cleanly', () => {
  withTempLog((logPath) => {
    const { run } = require('../scripts/prune-docker-cache');
    const fakeExecFn = () => 'Total reclaimed space: 0B\n';

    const result = run({ execFn: fakeExecFn });
    assert.equal(result.reclaimedBytes, 0);

    const record = readLastRecord(logPath);
    assert.equal(record.reclaimedBytes, 0);
  });
});

test('prune-docker-cache run(): propagates a real docker-CLI failure (e.g. daemon unreachable) rather than swallowing it', () => {
  withTempLog(() => {
    const { run } = require('../scripts/prune-docker-cache');
    const failingExecFn = () => {
      throw new Error('Cannot connect to the Docker daemon');
    };
    assert.throws(() => run({ execFn: failingExecFn }), /Cannot connect to the Docker daemon/);
  });
});
