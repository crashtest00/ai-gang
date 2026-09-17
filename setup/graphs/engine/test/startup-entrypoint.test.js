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

// ---- what the Initialization Agent printed ----
//
// The agent works unsupervised, and its own output used to go to the
// container's stdout and nowhere else. An agent that stopped partway —
// mid-step, exit status 0, the record still saying in-progress — left
// nothing in the checkout saying why: not its last message, not an
// error, no indication of whether it hit a failure at all. So the
// entrypoint keeps everything it prints in .ai-gang/agent.log, and when
// the record says the run did not finish it puts the end of that capture
// into the step log, where an operator is already looking.
//
// Both tests below drive the real entrypoint end to end against a
// temporary checkout, with a stub agent on PATH standing in for
// `claude`. Nothing here needs Docker: the entrypoint itself only
// requires the command to exist, and every step that would use it
// belongs to the agent.

const ENV_TEMPLATE = path.join(REPO_ROOT, '.env.template');
const SM_ENV_EXAMPLE = path.join(REPO_ROOT, 'services', 'scrummaster', '.env.example');

const PLATFORM_ENV = [
  'ANTHROPIC_API_KEY=sk-ant-test-not-a-real-key',
  'GH_TOKEN=github_pat_test_not_a_real_token',
  'AIGANG_ADMIN_USER=operator',
  'AIGANG_ADMIN_EMAIL=operator@example.invalid',
  'AIGANG_ADMIN_PASSWORD=a-test-only-password',
  'PGPASSWORD=a-test-only-pg-password',
  'DJANGO_SECRET_KEY=a-test-only-django-key',
].join('\n') + '\n';

// A checkout the real entrypoint can validate, derive from and record
// against, built from the repository's own scripts and validator. The
// two directories derive-env.sh writes into are this temporary tree's,
// not the repository's.
function runnableCheckout() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'aigang-agent-run-'));
  fs.symlinkSync(path.join(REPO_ROOT, 'scripts'), path.join(root, 'scripts'));
  fs.symlinkSync(path.join(REPO_ROOT, 'setup'), path.join(root, 'setup'));
  fs.mkdirSync(path.join(root, 'services', 'work-item-service'), { recursive: true });
  fs.mkdirSync(path.join(root, 'services', 'scrummaster'), { recursive: true });
  fs.copyFileSync(SM_ENV_EXAMPLE, path.join(root, 'services', 'scrummaster', '.env.example'));
  fs.copyFileSync(ENV_TEMPLATE, path.join(root, '.env.template'));
  fs.writeFileSync(path.join(root, '.env'), PLATFORM_ENV);
  fs.writeFileSync(path.join(root, 'ai-gang.config.json'), JSON.stringify({
    schemaVersion: 1,
    project: { name: 'a-project', type: 'web', stack: 'node-express' },
    repository: { url: 'https://github.com/an-org/a-repo.git' },
  }));
  return root;
}

// A stand-in for the Initialization Agent: whatever script body is given,
// with docker alongside it so the entrypoint's command check passes.
function agentBin(body) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'aigang-agent-bin-'));
  fs.writeFileSync(path.join(dir, 'docker'), '#!/bin/sh\nexit 0\n');
  fs.chmodSync(path.join(dir, 'docker'), 0o755);
  fs.writeFileSync(path.join(dir, 'claude'), body);
  fs.chmodSync(path.join(dir, 'claude'), 0o755);
  return dir;
}

function runToAgent(root, binDir) {
  return new Promise((resolve) => {
    const child = spawn('bash', [ENTRYPOINT], {
      cwd: root,
      stdio: ['ignore', 'pipe', 'pipe'],
      env: {
        ...process.env,
        PATH: `${binDir}:${process.env.PATH}`,
        HOME: fs.mkdtempSync(path.join(os.tmpdir(), 'aigang-home-')),
        // Exported, not just set for the entrypoint: the stub agent runs
        // status.sh, which must address this checkout's record and not
        // the repository's.
        AIGANG_ROOT: root,
        AIGANG_STATE_DIR: path.join(root, '.ai-gang'),
      },
    });
    let stdout = '';
    let stderr = '';
    child.stdout.on('data', (d) => { stdout += d; });
    child.stderr.on('data', (d) => { stderr += d; });
    child.on('close', (code) => resolve({ code, stdout, stderr }));
  });
}

// The line an operator would want and, until now, never got.
const LAST_WORDS = 'waiting for the work-item service to answer, and it never did';

// Stops while a step is still in progress, exit status 0 — the shape of
// the real thing, which ends its turn without saying so. The earlier
// lines are there to be dropped: the copy in the step log is bounded.
const STOPS_EARLY = `#!/bin/sh
i=1
while [ "$i" -le 60 ]; do
  echo "agent output line $i"
  i=$((i + 1))
done
echo "${LAST_WORDS}"
exit 0
`;

// Works through every step the way a real run does, so the record reads
// complete and the entrypoint exits 0.
const COMPLETES = `#!/bin/sh
set -e
steps="$AIGANG_ROOT/scripts/startup/steps.sh"
status="$AIGANG_ROOT/scripts/startup/status.sh"
"$steps" list | while IFS='|' read -r number id script description; do
  "$status" step-start "$id"
  "$status" step-done "$id"
done
echo "${LAST_WORDS}"
"$status" complete
exit 0
`;

test("an agent that stops before the last step leaves its output in the checkout", async () => {
  const root = runnableCheckout();
  const result = await runToAgent(root, agentBin(STOPS_EARLY));
  assert.notEqual(result.code, 0, 'a run whose record is not complete must exit nonzero');

  const stateDir = path.join(root, '.ai-gang');
  const agentLog = path.join(stateDir, 'agent.log');
  assert.ok(fs.existsSync(agentLog), `nothing captured the agent's output: ${result.stderr}`);
  const captured = fs.readFileSync(agentLog, 'utf8');
  assert.ok(captured.includes(LAST_WORDS), "the agent's last line is not in the capture");
  assert.ok(captured.includes('agent output line 1'), 'the capture is not the whole of what the agent printed');

  // Unfiltered agent output, so the file is the operator's alone.
  assert.equal(fs.statSync(agentLog).mode & 0o777, 0o600, 'the capture must be owner-readable only');

  // ...and it still reached the container's stdout as it happened.
  assert.ok(result.stdout.includes(LAST_WORDS), "the agent's output no longer streams to stdout");

  // The step log — the file the run's own diagnostic points at — carries
  // the end of it, after the line saying the run did not complete.
  const log = fs.readFileSync(path.join(stateDir, 'startup.log'), 'utf8');
  const notComplete = log.indexOf('initialization did not complete');
  assert.ok(notComplete > 0, `the step log does not report the incomplete run: ${log}`);
  assert.ok(log.indexOf(LAST_WORDS) > notComplete, "the agent's last line is not in the step log's tail");
  assert.ok(log.includes('agent.log'), 'the step log does not say where the whole capture is');

  // Bounded: the tail, not a second copy of the transcript.
  assert.equal(log.includes('agent output line 1\n'), false, 'the step log copied more than the tail');
  assert.ok(log.includes('agent output line 60'), 'the tail is shorter than the 40 lines it claims');
});

test('a completed run leaves the capture and keeps the step log free of a tail', async () => {
  const root = runnableCheckout();
  const result = await runToAgent(root, agentBin(COMPLETES));
  assert.equal(result.code, 0, `a completed run must exit 0: ${result.stderr}`);

  const stateDir = path.join(root, '.ai-gang');
  const captured = fs.readFileSync(path.join(stateDir, 'agent.log'), 'utf8');
  assert.ok(captured.includes(LAST_WORDS), "a completed run must keep the agent's output too");

  const log = fs.readFileSync(path.join(stateDir, 'startup.log'), 'utf8');
  assert.match(log, /initialization complete/);
  assert.equal(log.includes('initialization did not complete'), false);
  assert.equal(log.includes(LAST_WORDS), false,
    'a run that completed has no reason to copy the agent transcript into the step log');
});

test("a second run keeps the previous run's capture alongside its record and log", async () => {
  // The capture is a run's record like the other two, and the re-run
  // that follows a failure must not be what destroys it.
  const root = runnableCheckout();
  const first = await runToAgent(root, agentBin(STOPS_EARLY));
  assert.notEqual(first.code, 0);

  const second = await runToAgent(root, agentBin(COMPLETES));
  assert.equal(second.code, 0, second.stderr);

  const kept = fs.readFileSync(path.join(root, '.ai-gang', 'previous', 'agent.log'), 'utf8');
  assert.ok(kept.includes(LAST_WORDS), "the first run's capture must be kept");
});
