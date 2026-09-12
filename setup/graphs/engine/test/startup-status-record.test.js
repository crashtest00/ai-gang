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

function completeEveryStep(dir) {
  for (const line of steps('list').trim().split('\n')) {
    const id = line.split('|')[1];
    status(dir, 'step-start', id);
    status(dir, 'step-done', id);
  }
}

test('completion names the Django admin address', () => {
  const dir = makeStateDir();
  status(dir, 'init');
  completeEveryStep(dir);
  assert.equal(status(dir, 'complete').status, 0);
  const doc = record(dir);
  assert.equal(doc.state, 'complete');
  assert.equal(doc.adminUrl, status(dir, 'admin-url').stdout.trim());
  assert.match(doc.adminUrl, /^http:\/\/127\.0\.0\.1:9100\/django-admin\/$/);
});

test('the run cannot be marked complete while a step has not completed', () => {
  // The Initialization Agent can run this script, and the entrypoint
  // exits on the record rather than on the agent's exit code. If
  // `complete` took anyone's word for it, an agent that stopped early
  // could still make the container exit 0.
  const dir = makeStateDir();
  status(dir, 'init');
  const result = status(dir, 'complete');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /refusing to mark the run complete/);
  assert.match(result.stderr, /create-network/);
  assert.equal(record(dir).state, 'in-progress');
  assert.equal(record(dir).adminUrl, null);
});

test('one step short of the end is still short of the end', () => {
  const dir = makeStateDir();
  status(dir, 'init');
  completeEveryStep(dir);
  status(dir, 'step-start', 'confirm-health');   // re-opened, so not complete
  const result = status(dir, 'complete');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /confirm-health/);
  assert.notEqual(record(dir).state, 'complete');
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
  // Mid-run, from outside, straight off the file — which is what both
  // operator documents tell a reader to do. There is no reader
  // subcommand, and nothing needs one.
  const doc = JSON.parse(fs.readFileSync(path.join(dir, 'status.json'), 'utf8'));
  assert.equal(doc.step, 'create-network');
});

// ---- the log the container streams ----

test('a step logs to stdout when nothing is tailing the log file', () => {
  const dir = makeStateDir();
  const result = spawnSync('bash', ['-c',
    `source "${path.join(STARTUP_DIR, 'lib.sh')}"; log "a message"`], {
    encoding: 'utf8', timeout: 15000,
    env: { ...process.env, AIGANG_STATE_DIR: dir },
  });
  assert.equal(result.status, 0, result.stderr);
  assert.equal(result.stdout.trim(), '[startup] a message');
});

test('a step logs only to the file when the container is tailing it, so nothing prints twice', () => {
  // The AI Gang container tails .ai-gang/startup.log to its own stdout,
  // because the agent's tool output never reaches it. Printing directly
  // as well would show the operator every line of the run twice.
  const dir = makeStateDir();
  fs.writeFileSync(path.join(dir, 'startup.log'), '');
  const result = spawnSync('bash', ['-c',
    `source "${path.join(STARTUP_DIR, 'lib.sh')}"; log "a message"; warn "a warning"`], {
    encoding: 'utf8', timeout: 15000,
    env: { ...process.env, AIGANG_STATE_DIR: dir, AIGANG_LOG_TAILED: '1' },
  });
  assert.equal(result.status, 0, result.stderr);
  assert.equal(result.stdout, '');
  assert.equal(result.stderr, '');
  const logged = fs.readFileSync(path.join(dir, 'startup.log'), 'utf8');
  assert.match(logged, /\[startup\] a message/);
  assert.match(logged, /\[startup\] a warning/);
});

test('a step that failed and is re-run clears the failure, so the record blames the right thing', () => {
  // The agent is allowed to put a failed step right and re-run it. The
  // record has to follow: a live run recovered a Redis failure, failed
  // later for an unrelated reason, and reported the Redis failure as the
  // cause because nothing had cleared it.
  const dir = makeStateDir();
  status(dir, 'init');
  status(dir, 'step-start', 'start-redis');
  status(dir, 'fail', 'Redis did not start');
  assert.equal(record(dir).state, 'failed');

  status(dir, 'step-start', 'start-redis');
  let doc = record(dir);
  assert.equal(doc.state, 'in-progress');
  assert.equal(doc.error, null);
  assert.equal(doc.steps.find((s) => s.id === 'start-redis').state, 'in-progress');

  status(dir, 'step-done', 'start-redis');
  status(dir, 'step-start', 'start-work-item-service');
  status(dir, 'fail', 'the work-item service did not answer /health');
  doc = record(dir);
  assert.equal(doc.error, 'the work-item service did not answer /health');
});

test('a re-run step does not keep a finish time from the attempt that failed', () => {
  const dir = makeStateDir();
  status(dir, 'init');
  status(dir, 'step-start', 'start-redis');
  status(dir, 'step-done', 'start-redis');
  status(dir, 'step-start', 'start-redis');
  assert.equal(record(dir).steps.find((s) => s.id === 'start-redis').finishedAt, null);
});
