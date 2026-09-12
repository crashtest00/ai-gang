'use strict';

// scripts/startup/status.sh — the record the flow's progress is read from,
// in the checkout and outside every container, and the ordered step list
// it is built from (scripts/startup/steps.sh).

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const REPO_ROOT = path.join(__dirname, '..', '..', '..', '..');
const STARTUP_DIR = path.join(REPO_ROOT, 'scripts', 'startup');
const STATUS = path.join(STARTUP_DIR, 'status.sh');
const STEPS = path.join(STARTUP_DIR, 'steps.sh');

function makeStateDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'aigang-status-'));
}

function status(stateDir, ...args) {
  return spawnSync('bash', [STATUS, ...args], {
    encoding: 'utf8',
    timeout: 15000,
    env: { ...process.env, AIGANG_STATE_DIR: stateDir },
  });
}

function steps(...args) {
  const result = spawnSync('bash', [STEPS, ...args], { encoding: 'utf8', timeout: 15000 });
  assert.equal(result.status, 0, result.stderr);
  return result.stdout;
}

function record(stateDir) {
  return JSON.parse(fs.readFileSync(path.join(stateDir, 'status.json'), 'utf8'));
}

// ---- the step list ----

test('the step list is the ordered initialization sequence, numbered from one', () => {
  const rows = steps('list').trim().split('\n').map((l) => l.split('|'));
  assert.deepEqual(
    rows.map((r) => r[1]),
    [
      'create-network',
      'start-redis',
      'start-work-item-service',
      'create-admin',
      'start-scrummaster',
      'initialize-project',
      'install-project-dockerfile',
      'start-project',
      'confirm-health',
    ]
  );
  rows.forEach((row, i) => {
    assert.equal(Number(row[0]), i + 1);
    assert.notEqual(row[3].trim(), '');
  });
});

test('every listed step has an executable script at the path it names', () => {
  for (const line of steps('list').trim().split('\n')) {
    const [, id, script] = line.split('|');
    const full = path.join(REPO_ROOT, script);
    assert.ok(fs.existsSync(full), `${id} names ${script}, which does not exist`);
    assert.ok(fs.statSync(full).mode & 0o111, `${script} is not executable`);
  }
});

test('an unknown step id is an error, not an empty answer', () => {
  const result = spawnSync('bash', [STEPS, 'number', 'no-such-step'], { encoding: 'utf8', timeout: 15000 });
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /unknown step id/);
});

// ---- the record ----

test('init creates a record listing every step as pending', () => {
  const dir = makeStateDir();
  assert.equal(status(dir, 'init').status, 0);
  const doc = record(dir);
  assert.equal(doc.state, 'in-progress');
  assert.equal(doc.phase, 'preflight');
  assert.equal(doc.adminUrl, null);
  assert.equal(doc.steps.length, steps('list').trim().split('\n').length);
  assert.ok(doc.steps.every((s) => s.state === 'pending'));
});

test('the record names the step in progress while it runs, and marks it complete after', () => {
  const dir = makeStateDir();
  status(dir, 'init');
  status(dir, 'step-start', 'start-redis');

  let doc = record(dir);
  assert.equal(doc.step, 'start-redis');
  assert.equal(doc.phase, 'initializing');
  assert.equal(doc.steps.find((s) => s.id === 'start-redis').state, 'in-progress');
  assert.equal(doc.steps.find((s) => s.id === 'create-network').state, 'pending');

  status(dir, 'step-done', 'start-redis');
  doc = record(dir);
  assert.equal(doc.steps.find((s) => s.id === 'start-redis').state, 'complete');
  assert.notEqual(doc.steps.find((s) => s.id === 'start-redis').finishedAt, null);
});

test("each service's health is recorded by name", () => {
  const dir = makeStateDir();
  status(dir, 'init');
  status(dir, 'service', 'redis', 'healthy');
  status(dir, 'service', 'hello-web', 'unhealthy');
  assert.deepEqual(record(dir).services, { redis: 'healthy', 'hello-web': 'unhealthy' });
});

test('completion names the Django admin address', () => {
  const dir = makeStateDir();
  status(dir, 'init');
  status(dir, 'complete');
  const doc = record(dir);
  assert.equal(doc.state, 'complete');
  assert.equal(doc.adminUrl, status(dir, 'admin-url').stdout.trim());
  assert.match(doc.adminUrl, /^http:\/\/127\.0\.0\.1:9100\/django-admin\/$/);
});

test('failure records the reason and leaves the run non-complete', () => {
  const dir = makeStateDir();
  status(dir, 'init');
  status(dir, 'step-start', 'start-scrummaster');
  status(dir, 'fail', 'ScrumMaster did not answer /health');
  const doc = record(dir);
  assert.equal(doc.state, 'failed');
  assert.equal(doc.error, 'ScrumMaster did not answer /health');
  assert.equal(status(dir, 'state').stdout.trim(), 'failed');
});

test('acting on a record that does not exist yet is refused, not silently invented', () => {
  const dir = makeStateDir();
  const result = status(dir, 'step-start', 'start-redis');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /no status record/);
});

test('the record is plain JSON a second shell can read at any point', () => {
  const dir = makeStateDir();
  status(dir, 'init');
  status(dir, 'step-start', 'create-network');
  // Mid-run, from outside: parses, and says where the run has got to.
  const doc = JSON.parse(status(dir, 'show').stdout);
  assert.equal(doc.step, 'create-network');
});
