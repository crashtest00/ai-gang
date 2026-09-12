'use strict';

// Drives the real cli.js --platform entrypoint as a subprocess — the one
// the AI Gang container's entrypoint and scripts/startup/validate-config.sh
// actually call — rather than the function behind it, so what a shell
// reads back off stdout is what is asserted here.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const REPO_ROOT = path.join(__dirname, '..', '..', '..', '..');
const CLI = path.join(__dirname, '..', 'lib', 'config', 'cli.js');
const TEMPLATE = path.join(REPO_ROOT, 'ai-gang.config.template.json');

function run(args) {
  return spawnSync(process.execPath, [CLI, ...args], { encoding: 'utf8', timeout: 15000 });
}

function writeConfig(content) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'aigang-platform-cli-'));
  const file = path.join(dir, 'ai-gang.config.json');
  fs.writeFileSync(file, typeof content === 'string' ? content : JSON.stringify(content));
  return file;
}

const VALID = {
  schemaVersion: 1,
  project: { name: 'acceptance-project', type: 'web', stack: 'node-express' },
  repository: { url: 'https://github.com/an-org/a-repo.git' },
};

test('--platform prints the four decisions as KEY=value lines and exits 0', () => {
  const result = run(['--platform', writeConfig(VALID)]);
  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(result.stdout.trim().split('\n'), [
    'PROJECT_NAME=acceptance-project',
    'PROJECT_TYPE=web',
    'PROJECT_STACK=node-express',
    'REPOSITORY_URL=https://github.com/an-org/a-repo.git',
  ]);
});

test('--platform on the unedited template exits 1, names every unfilled field, and prints nothing to stdout', () => {
  const result = run(['--platform', TEMPLATE]);
  assert.equal(result.status, 1);
  assert.equal(result.stdout, '');
  for (const field of ['project.name', 'project.type', 'project.stack', 'repository.url']) {
    assert.ok(result.stderr.includes(`"${field}"`), `expected ${field} in: ${result.stderr}`);
  }
});

test('--platform refuses a configuration with no repository object', () => {
  const config = JSON.parse(JSON.stringify(VALID));
  delete config.repository;
  const result = run(['--platform', writeConfig(config)]);
  assert.equal(result.status, 1);
  assert.match(result.stderr, /missing required field "repository"/);
});

test('without --platform the same configuration is accepted without the repository object', () => {
  const config = JSON.parse(JSON.stringify(VALID));
  delete config.repository;
  const result = run([writeConfig(config)]);
  assert.equal(result.status, 0, result.stderr);
  assert.equal(result.stdout.includes('REPOSITORY_URL='), false);
});

test('without --platform a repository object is passed through as REPOSITORY_URL', () => {
  const result = run([writeConfig(VALID)]);
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /^REPOSITORY_URL=https:\/\/github\.com\/an-org\/a-repo\.git$/m);
});

test('a missing file is reported, not thrown', () => {
  const result = run(['--platform', path.join(os.tmpdir(), 'definitely-not-here.json')]);
  assert.equal(result.status, 1);
  assert.match(result.stderr, /config file not found/);
});

test('no argument is a usage error', () => {
  const result = run(['--platform']);
  assert.equal(result.status, 2);
  assert.match(result.stderr, /usage: cli\.js \[--platform\] <config-file>/);
});
