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
const { spawnSync } = require('node:child_process');

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
