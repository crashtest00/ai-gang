'use strict';

// scripts/startup/config-identity.sh — the checkout-level record of the
// configuration a checkout was initialized with, and the refusal of a run
// whose configuration differs.
//
// The case that matters is a changed project.name. The per-project record
// scripts/init-project.sh writes is keyed by the project directory, so a
// renamed project simply addresses a different directory and reads as a
// brand-new one; only a record held by the checkout can see that this
// checkout has already been initialized under another name.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const REPO_ROOT = path.join(__dirname, '..', '..', '..', '..');
const IDENTITY = path.join(REPO_ROOT, 'scripts', 'startup', 'config-identity.sh');

const BASE = {
  schemaVersion: 1,
  project: { name: 'acceptance-project', type: 'web', stack: 'node-express' },
  repository: { url: 'https://github.com/an-org/a-repo.git' },
};

function makeFixture(config = BASE) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'aigang-identity-'));
  const configFile = path.join(dir, 'ai-gang.config.json');
  fs.writeFileSync(configFile, JSON.stringify(config));
  return { dir, configFile, stateDir: path.join(dir, '.ai-gang') };
}

// AIGANG_ROOT stays the real checkout — the validator this step calls
// lives there — while the configuration and the record are redirected
// into the fixture.
function run(fixture, command, configOverride) {
  if (configOverride) fs.writeFileSync(fixture.configFile, JSON.stringify(configOverride));
  return spawnSync('bash', [IDENTITY, command], {
    encoding: 'utf8',
    timeout: 20000,
    env: {
      ...process.env,
      AIGANG_CONFIG_FILE: fixture.configFile,
      AIGANG_STATE_DIR: fixture.stateDir,
    },
  });
}

function recordFile(fixture) {
  return path.join(fixture.stateDir, 'config-identity.json');
}

test('a first run has nothing recorded, and recording writes the normalized configuration', () => {
  const fixture = makeFixture();
  const checked = run(fixture, 'check');
  assert.equal(checked.status, 0, checked.stderr);
  assert.match(checked.stdout, /first run/);
  assert.equal(fs.existsSync(recordFile(fixture)), false, 'check must not write the record');

  assert.equal(run(fixture, 'record').status, 0);
  const doc = JSON.parse(fs.readFileSync(recordFile(fixture), 'utf8'));
  assert.equal(doc.project.name, 'acceptance-project');
  assert.equal(doc.project.type, 'web');
  assert.equal(doc.project.stack, 'node-express');
  assert.equal(doc.repository.url, 'https://github.com/an-org/a-repo.git');
  assert.match(doc.initializedAt, /^\d{4}-\d{2}-\d{2}T/);
});

test('an unchanged configuration is accepted on every later run, and the record is not rewritten', () => {
  const fixture = makeFixture();
  run(fixture, 'record');
  const before = fs.readFileSync(recordFile(fixture), 'utf8');

  const second = run(fixture, 'check');
  assert.equal(second.status, 0, second.stderr);
  assert.match(second.stdout, /resuming this checkout's existing installation/);
  assert.equal(run(fixture, 'record').status, 0);
  assert.equal(fs.readFileSync(recordFile(fixture), 'utf8'), before);
});

test('a changed project name is refused, and nothing is rewritten', () => {
  const fixture = makeFixture();
  run(fixture, 'record');
  const before = fs.readFileSync(recordFile(fixture), 'utf8');

  const renamed = { ...BASE, project: { ...BASE.project, name: 'a-different-project' } };
  const result = run(fixture, 'check', renamed);
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /already initialized with a different configuration/);
  assert.match(result.stderr, /a-different-project/);
  assert.match(result.stderr, /Nothing has been changed/);
  assert.equal(fs.readFileSync(recordFile(fixture), 'utf8'), before);
});

test('a changed repository URL is refused too', () => {
  const fixture = makeFixture();
  run(fixture, 'record');
  const moved = { ...BASE, repository: { url: 'https://github.com/an-org/somewhere-else.git' } };
  const result = run(fixture, 'check', moved);
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /somewhere-else/);
});

test('record refuses a changed configuration as firmly as check does', () => {
  const fixture = makeFixture();
  run(fixture, 'record');
  const renamed = { ...BASE, project: { ...BASE.project, name: 'a-different-project' } };
  const result = run(fixture, 'record', renamed);
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /already initialized with a different configuration/);
});

test('an invalid configuration fails validation here, before anything is recorded', () => {
  const fixture = makeFixture({ schemaVersion: 1, project: { name: 'x', type: 'web', stack: 'nope' } });
  const result = run(fixture, 'record');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /not a supported stack profile|missing required field "repository"/);
  assert.equal(fs.existsSync(recordFile(fixture)), false);
});
