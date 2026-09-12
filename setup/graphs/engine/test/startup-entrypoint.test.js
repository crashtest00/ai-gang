'use strict';

// scripts/startup/entrypoint.sh's one job before anything else: find the
// checkout.
//
// The entrypoint is the only file baked into the AI Gang container's
// image; every step it runs comes from the checkout mounted at the
// working directory. So it must resolve the steps relative to that
// working directory and not relative to itself — a copy of this file at
// /opt/ai-gang must still run the steps in the operator's own checkout.
// Resolving them next to itself instead fails at the first step, which is
// how this was found on a real run.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync, spawn } = require('node:child_process');

const REPO_ROOT = path.join(__dirname, '..', '..', '..', '..');
const ENTRYPOINT = path.join(REPO_ROOT, 'scripts', 'startup', 'entrypoint.sh');

// The image's own copy: the entrypoint, alone, somewhere else entirely.
function installedCopy() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'aigang-image-'));
  const copy = path.join(dir, 'entrypoint.sh');
  fs.copyFileSync(ENTRYPOINT, copy);
  fs.chmodSync(copy, 0o755);
  return copy;
}

function run(entrypoint, cwd) {
  return spawnSync('bash', [entrypoint], {
    cwd,
    encoding: 'utf8',
    timeout: 30000,
    input: '',
    env: {
      ...process.env,
      HOME: fs.mkdtempSync(path.join(os.tmpdir(), 'aigang-home-')),
      // The run is real, so its records go to a temp directory rather
      // than into the checkout it is pointed at.
      AIGANG_STATE_DIR: fs.mkdtempSync(path.join(os.tmpdir(), 'aigang-state-')),
    },
  });
}

test('a copy of the entrypoint outside the checkout still finds the checkout it is run in', () => {
  const result = run(installedCopy(), REPO_ROOT);
  assert.equal(
    result.stderr.includes('does not look like an AI Gang checkout'),
    false,
    `the entrypoint failed to locate the checkout: ${result.stderr}`
  );
  assert.match(result.stdout, /\[startup\] AI Gang platform startup/);
  assert.ok(
    result.stdout.includes(`checkout: ${REPO_ROOT}`),
    `expected the working directory to be reported as the checkout: ${result.stdout}`
  );
});

test('run somewhere that is not a checkout, it says so instead of failing obscurely', () => {
  const elsewhere = fs.mkdtempSync(path.join(os.tmpdir(), 'aigang-not-a-checkout-'));
  const result = run(installedCopy(), elsewhere);
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /does not look like an AI Gang checkout/);
  assert.match(result.stderr, /from the root of the AI Gang repository/);
});

test('it names every command the flow depends on', () => {
  // Each of these is something a step actually runs; a missing one has to
  // be reported by name, before the run rather than partway through it.
  const text = fs.readFileSync(ENTRYPOINT, 'utf8');
  for (const command of ['docker', 'node', 'jq', 'git', 'claude']) {
    assert.match(text, new RegExp(`require_command ${command}\\b`));
  }
});

test('it validates the configuration and the environment before deriving or starting anything', () => {
  const text = fs.readFileSync(ENTRYPOINT, 'utf8');
  const order = ['validate-config.sh', 'validate-env.sh', 'config-identity.sh', 'derive-env.sh', 'claude --print'];
  let cursor = -1;
  for (const marker of order) {
    const at = text.indexOf(marker, cursor + 1);
    assert.ok(at > cursor, `${marker} must come after the step before it`);
    cursor = at;
  }
});

test('it exits on the status record rather than on the agent\'s own exit code', () => {
  // An agent that stops early, or reports success it did not achieve,
  // must not be able to make the container exit 0.
  const text = fs.readFileSync(ENTRYPOINT, 'utf8');
  assert.match(text, /RUN_STATE="\$\(status state/);
  assert.match(text, /if \[\[ "\$RUN_STATE" == "complete" \]\]; then\n\s+log[^\n]*\n\s+exit 0/);
  assert.match(text, /exit 1\n$/);
});

// ---- the pre-flight diagnostic reaching the operator's terminal ----
//
// While the container is tailing .ai-gang/startup.log to its own stdout,
// that tail is the only path to it: lib.sh's log/warn stop printing
// directly (startup-status-record.test.js covers that rule). So a
// validation failure's diagnostic reaches the operator only if the tail
// is given the chance to print it before the container exits. Killing it
// on the way out, as the failure paths once did, lost the diagnostic
// about half the time.

// A pre-flight run needs `docker` and `claude` on PATH to get as far as
// validating the configuration; neither is invoked before the failure
// under test, so stubs are enough and the test needs no Docker.
function stubBin() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'aigang-stub-bin-'));
  for (const name of ['docker', 'claude']) {
    const file = path.join(dir, name);
    fs.writeFileSync(file, '#!/bin/sh\nexit 0\n');
    fs.chmodSync(file, 0o755);
  }
  return dir;
}

const STUB_BIN = stubBin();

function invalidConfig() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'aigang-bad-config-'));
  const file = path.join(dir, 'ai-gang.config.json');
  fs.writeFileSync(file, JSON.stringify({
    schemaVersion: 1,
    project: { name: 'a-project', type: 'web', stack: 'not-a-stack' },
    repository: { url: 'https://github.com/an-org/a-repo.git' },
  }));
  return file;
}

// Runs the real entrypoint as far as configuration validation, which
// fails. Everything after that — the agent, every step — is never
// reached.
function runPreflight(configFile, stateDir) {
  return new Promise((resolve) => {
    const child = spawn('bash', [ENTRYPOINT], {
      cwd: REPO_ROOT,
      stdio: ['ignore', 'pipe', 'pipe'],
      env: {
        ...process.env,
        PATH: `${STUB_BIN}:${process.env.PATH}`,
        HOME: fs.mkdtempSync(path.join(os.tmpdir(), 'aigang-home-')),
        AIGANG_STATE_DIR: stateDir,
        AIGANG_CONFIG_FILE: configFile,
      },
    });
    let stdout = '';
    let stderr = '';
    child.stdout.on('data', (d) => { stdout += d; });
    child.stderr.on('data', (d) => { stderr += d; });
    child.on('close', (code) => resolve({ code, stdout, stderr }));
  });
}

function freshStateDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'aigang-preflight-'));
}

test('a pre-flight failure names the field on stdout every single time', async () => {
  const configFile = invalidConfig();
  const runs = await Promise.all(
    Array.from({ length: 20 }, () => runPreflight(configFile, freshStateDir()))
  );

  const missed = runs.filter((r) => !r.stdout.includes('"project.stack"'));
  assert.equal(
    missed.length,
    0,
    `${missed.length} of ${runs.length} runs did not print the failing field to stdout`
  );
  for (const result of runs) {
    assert.notEqual(result.code, 0, 'an invalid configuration must exit nonzero');
    assert.match(result.stdout, /not a supported stack profile/);
  }
});

// ---- the records a run leaves behind ----

test('a failed run leaves its record and log in the checkout', async () => {
  const stateDir = freshStateDir();
  const result = await runPreflight(invalidConfig(), stateDir);
  assert.notEqual(result.code, 0);

  const record = JSON.parse(fs.readFileSync(path.join(stateDir, 'status.json'), 'utf8'));
  assert.equal(record.state, 'failed');
  assert.match(record.error, /not a valid platform configuration/);
  assert.match(fs.readFileSync(path.join(stateDir, 'startup.log'), 'utf8'), /not a supported stack profile/);
  assert.equal(fs.existsSync(path.join(stateDir, 'previous')), false, 'a first run has no previous run to keep');
  assert.equal(result.stdout.includes("previous run's"), false);
});

test("a second run keeps the previous run's record and log rather than deleting them", async () => {
  // The evidence of a failed run has to survive the re-run that follows
  // it: that re-run is usually the first thing an operator does.
  const stateDir = freshStateDir();
  const first = await runPreflight(invalidConfig(), stateDir);
  assert.notEqual(first.code, 0);

  const secondConfig = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'aigang-bad-config-')), 'ai-gang.config.json');
  fs.writeFileSync(secondConfig, JSON.stringify({
    schemaVersion: 1,
    project: { name: 'a-project', type: 'not-a-target', stack: 'node-express' },
    repository: { url: 'https://github.com/an-org/a-repo.git' },
  }));
  const second = await runPreflight(secondConfig, stateDir);
  assert.notEqual(second.code, 0);

  const previousLog = fs.readFileSync(path.join(stateDir, 'previous', 'startup.log'), 'utf8');
  assert.match(previousLog, /not a supported stack profile/, "the first run's log must be kept");
  const previousRecord = JSON.parse(fs.readFileSync(path.join(stateDir, 'previous', 'status.json'), 'utf8'));
  assert.equal(previousRecord.state, 'failed');

  // ...and this run's own records are the current ones.
  assert.match(fs.readFileSync(path.join(stateDir, 'startup.log'), 'utf8'), /not a supported deployment target/);
  assert.match(second.stdout, /the previous run's record and log are kept in/);
});

test('a root-owned checkout is reported in the run log rather than passing silently', async () => {
  // The privilege drop only happens when the checkout belongs to
  // somebody other than root. When it does not, the whole run — the
  // Initialization Agent included — is root, and every file it creates
  // in the operator's checkout comes back owned by root. The suite does
  // not run as root, so the branch itself is checked in the source and
  // its absence is checked by running: an unprivileged run must not
  // print the warning.
  const text = fs.readFileSync(ENTRYPOINT, 'utf8');
  assert.match(text, /AIGANG_ROOT_CHECKOUT=1/);
  assert.match(text, /if \[\[ "\$\{AIGANG_ROOT_CHECKOUT:-0\}" == "1" \]\]; then\n\s+warn /);
  assert.match(text, /warn "this checkout is owned by root/);

  const result = await runPreflight(invalidConfig(), freshStateDir());
  assert.equal(result.stdout.includes('owned by root'), false,
    'an unprivileged run must not claim it is running as root');
});
