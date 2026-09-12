'use strict';

// scripts/startup/create-admin.sh — the one step that handles the Django
// admin password.
//
// The whole discipline of this flow is that .env is the only place a
// secret lives. A secret handed to a command as an argument is also in
// the host's process table, readable by any local user for the life of
// the call, so this drives the real script with a stand-in `docker` that
// records exactly what the process table would have shown.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const REPO_ROOT = path.join(__dirname, '..', '..', '..', '..');
const STARTUP_DIR = path.join(REPO_ROOT, 'scripts', 'startup');
const SECRET = 'a-password-no-one-else-should-see';

// A `docker` that records its own command line the way `ps` would read
// it, plus the mode and contents of any file it was pointed at.
function stubDocker(dir) {
  const bin = path.join(dir, 'bin');
  fs.mkdirSync(bin, { recursive: true });
  const stub = path.join(bin, 'docker');
  fs.writeFileSync(stub, [
    '#!/usr/bin/env bash',
    'tr "\\0" "\\n" < /proc/$$/cmdline > "$AIGANG_TEST_ARGV"',
    'prev=""',
    'for arg in "$@"; do',
    '  if [[ "$prev" == "--env-file" ]]; then',
    '    printf "%s\\n" "$arg" > "$AIGANG_TEST_ENVFILE_PATH"',
    '    stat -c %a "$arg" > "$AIGANG_TEST_ENVFILE_MODE"',
    '    cat "$arg" > "$AIGANG_TEST_ENVFILE_BODY"',
    '  fi',
    '  prev="$arg"',
    'done',
    'exit 0',
    '',
  ].join('\n'));
  fs.chmodSync(stub, 0o755);
  return bin;
}

function runCreateAdmin() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'aigang-admin-'));
  const stateDir = path.join(dir, 'state');
  fs.mkdirSync(stateDir);
  const envFile = path.join(dir, '.env');
  fs.writeFileSync(envFile, [
    'ANTHROPIC_API_KEY=sk-not-a-real-key',
    'GH_TOKEN=github_pat_test_not_a_real_token',
    'AIGANG_ADMIN_USER=admin',
    'AIGANG_ADMIN_EMAIL=admin@example.invalid',
    `AIGANG_ADMIN_PASSWORD=${SECRET}`,
    'PGPASSWORD=not-a-real-password',
    'DJANGO_SECRET_KEY=not-a-real-key',
    '',
  ].join('\n'));

  const paths = {
    argv: path.join(dir, 'argv'),
    envFilePath: path.join(dir, 'envfile-path'),
    envFileMode: path.join(dir, 'envfile-mode'),
    envFileBody: path.join(dir, 'envfile-body'),
  };

  const env = {
    ...process.env,
    PATH: `${stubDocker(dir)}:${process.env.PATH}`,
    AIGANG_STATE_DIR: stateDir,
    AIGANG_ENV_FILE: envFile,
    AIGANG_TEST_ARGV: paths.argv,
    AIGANG_TEST_ENVFILE_PATH: paths.envFilePath,
    AIGANG_TEST_ENVFILE_MODE: paths.envFileMode,
    AIGANG_TEST_ENVFILE_BODY: paths.envFileBody,
  };

  const init = spawnSync('bash', [path.join(STARTUP_DIR, 'status.sh'), 'init'], { env, encoding: 'utf8', timeout: 15000 });
  assert.equal(init.status, 0, init.stderr);

  const result = spawnSync('bash', [path.join(STARTUP_DIR, 'create-admin.sh')], {
    env, encoding: 'utf8', timeout: 30000,
  });
  return { result, paths, env };
}

test('the admin password is never on a command line, and never in the process table', () => {
  const { result, paths } = runCreateAdmin();
  assert.equal(result.status, 0, `${result.stdout}\n${result.stderr}`);

  const argv = fs.readFileSync(paths.argv, 'utf8');
  assert.ok(argv.includes('ensure_admin'), `the stand-in docker was not invoked: ${argv}`);
  assert.equal(argv.includes(SECRET), false, `the password was on the docker command line:\n${argv}`);
  assert.ok(argv.includes('--env-file'), 'the values must be handed over in a file');

  // Nor anywhere the run itself can be read from.
  assert.equal(`${result.stdout}${result.stderr}`.includes(SECRET), false);
});

test('the file the values are handed over in is private, and is removed afterwards', () => {
  const { result, paths } = runCreateAdmin();
  assert.equal(result.status, 0, `${result.stdout}\n${result.stderr}`);

  assert.equal(fs.readFileSync(paths.envFileMode, 'utf8').trim(), '600');
  const body = fs.readFileSync(paths.envFileBody, 'utf8');
  assert.match(body, new RegExp(`^AIGANG_ADMIN_PASSWORD=${SECRET}$`, 'm'));
  assert.match(body, /^AIGANG_ADMIN_USER=admin$/m);
  assert.match(body, /^AIGANG_ADMIN_EMAIL=admin@example\.invalid$/m);

  const handedOver = fs.readFileSync(paths.envFilePath, 'utf8').trim();
  assert.equal(fs.existsSync(handedOver), false, 'the file must not outlive the command');
});

test('a failure removes the file too, rather than leaving the password on disk', () => {
  const { result, paths, env } = runCreateAdmin();
  assert.equal(result.status, 0);
  const previous = fs.readFileSync(paths.envFilePath, 'utf8').trim();
  assert.equal(fs.existsSync(previous), false);

  // Now the same step with a `docker` that fails.
  const failing = path.join(path.dirname(env.PATH.split(':')[0]), 'bin', 'docker');
  fs.writeFileSync(failing, [
    '#!/usr/bin/env bash',
    'prev=""',
    'for arg in "$@"; do',
    '  if [[ "$prev" == "--env-file" ]]; then printf "%s\\n" "$arg" > "$AIGANG_TEST_ENVFILE_PATH"; fi',
    '  prev="$arg"',
    'done',
    'exit 1',
    '',
  ].join('\n'));
  fs.chmodSync(failing, 0o755);

  const second = spawnSync('bash', [path.join(STARTUP_DIR, 'create-admin.sh')], {
    env, encoding: 'utf8', timeout: 30000,
  });
  assert.notEqual(second.status, 0);
  const leftBehind = fs.readFileSync(paths.envFilePath, 'utf8').trim();
  assert.equal(fs.existsSync(leftBehind), false, 'a failed step must not leave the password on disk');
});
