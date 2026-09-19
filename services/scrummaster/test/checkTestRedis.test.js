'use strict';

// The guard `npm test` runs before anything else (scripts/check-test-redis.js).
// Exercised as the test run itself invokes it — spawned as a process, with its
// real exit status and its real output — because the whole point of it is what
// a caller sees when the prerequisite is missing.

const path = require('node:path');
const { execFile } = require('node:child_process');
const { test } = require('node:test');
const assert = require('node:assert/strict');

const SCRIPT = path.join(__dirname, '..', 'scripts', 'check-test-redis.js');

function run(env) {
  return new Promise(resolve => {
    execFile(process.execPath, [SCRIPT], { env: { ...process.env, ...env } }, (err, stdout, stderr) => {
      resolve({ code: err ? err.code : 0, stdout, stderr });
    });
  });
}

test('the check passes when the Redis the tests need is listening', async () => {
  const result = await run({
    REDIS_TEST_URL: '',
    REDIS_TEST_HOST: process.env.REDIS_TEST_HOST || 'localhost',
    REDIS_TEST_PORT: process.env.REDIS_TEST_PORT || '16399',
  });
  assert.equal(result.code, 0, `the check must pass against the running test Redis, got: ${result.stderr}`);
});

test('the check fails fast and names what to start when nothing is listening', async () => {
  // A port nothing in this repository binds.
  const result = await run({ REDIS_TEST_URL: '', REDIS_TEST_HOST: 'localhost', REDIS_TEST_PORT: '16498' });

  assert.equal(result.code, 1, 'a missing prerequisite must fail the run, not let it hang');
  assert.match(result.stderr, /localhost:16498/, 'it must say which address it could not reach');
  assert.match(
    result.stderr,
    /docker compose -f services\/work-item-service\/docker-compose\.test\.yml up --wait/,
    'and name the one thing that provides it'
  );
});
