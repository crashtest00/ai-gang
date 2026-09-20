'use strict';

// The two steps that stand the project container up from configuration
// alone, driven for real: the stack profile -> image template mapping
// (scripts/startup/stack-dockerfile.sh) and the step that installs it
// (scripts/startup/install-project-dockerfile.sh).
//
// scripts/init-project.sh writes the project's docker-compose.yml with
// `build: .` and no Dockerfile, and tells the operator to copy one in by
// hand. That hand step is what this replaces.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const REPO_ROOT = path.join(__dirname, '..', '..', '..', '..');
const STARTUP_DIR = path.join(REPO_ROOT, 'scripts', 'startup');
const MAP = path.join(STARTUP_DIR, 'stack-dockerfile.sh');
const INSTALL = path.join(STARTUP_DIR, 'install-project-dockerfile.sh');
const { listSupportedTargets, listSupportedStacks } = require('../lib/config/catalog');

const CONFIG = {
  schemaVersion: 1,
  project: { name: 'acceptance-project', type: 'web', stack: 'node-express' },
  repository: { url: 'https://github.com/an-org/a-repo.git' },
};

function mapping(type, stack) {
  return spawnSync('bash', [MAP, type, stack], { encoding: 'utf8', timeout: 15000 });
}

// A temporary checkout carrying only what this step reads: the templates,
// the configuration, the project directory, and the state directory.
function makeRoot({ projectDir = true, existingDockerfile = null, config = CONFIG } = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'aigang-project-dockerfile-'));
  fs.mkdirSync(path.join(root, 'Docker Templates'), { recursive: true });
  for (const file of fs.readdirSync(path.join(REPO_ROOT, 'Docker Templates'))) {
    fs.copyFileSync(
      path.join(REPO_ROOT, 'Docker Templates', file),
      path.join(root, 'Docker Templates', file)
    );
  }
  fs.writeFileSync(path.join(root, 'ai-gang.config.json'), JSON.stringify(config));
  if (projectDir) {
    fs.mkdirSync(path.join(root, 'projects', config.project.name), { recursive: true });
    if (existingDockerfile !== null) {
      fs.writeFileSync(path.join(root, 'projects', config.project.name, 'Dockerfile'), existingDockerfile);
    }
  }
  // The validator this step reads its decisions back through lives in the
  // real checkout, so it is linked rather than copied.
  fs.mkdirSync(path.join(root, 'setup', 'graphs', 'engine', 'lib'), { recursive: true });
  fs.symlinkSync(
    path.join(REPO_ROOT, 'setup', 'graphs', 'engine', 'lib', 'config'),
    path.join(root, 'setup', 'graphs', 'engine', 'lib', 'config')
  );
  return root;
}

function install(root) {
  const stateDir = path.join(root, '.ai-gang');
  spawnSync('bash', [path.join(STARTUP_DIR, 'status.sh'), 'init'], {
    encoding: 'utf8',
    env: { ...process.env, AIGANG_STATE_DIR: stateDir },
    timeout: 15000,
  });
  return spawnSync('bash', [INSTALL], {
    encoding: 'utf8',
    timeout: 20000,
    env: { ...process.env, AIGANG_ROOT: root, AIGANG_STATE_DIR: stateDir },
  });
}

// ---- the mapping ----

test('the supported profile maps to the Node image template, which exists', () => {
  const result = mapping('web', 'node-express');
  assert.equal(result.status, 0, result.stderr);
  const template = result.stdout.trim();
  assert.equal(template, 'Docker Templates/Dockerfile-node.template');
  assert.ok(fs.existsSync(path.join(REPO_ROOT, template)));
});

test('every profile the shipped catalog supports has a template here', () => {
  // The catalog decides what a configuration may say; this mapping has to
  // cover all of it, or a configuration could validate and then have
  // nothing to build the project container from.
  for (const type of listSupportedTargets()) {
    for (const stack of listSupportedStacks(type)) {
      const result = mapping(type, stack);
      assert.equal(result.status, 0, `${type}/${stack} has no image template: ${result.stderr}`);
      assert.ok(fs.existsSync(path.join(REPO_ROOT, result.stdout.trim())));
    }
  }
});

test('an unmapped profile fails, naming what it does know', () => {
  const result = mapping('mobile', 'expo');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /no container image template/);
  assert.match(result.stderr, /web\/node-express/);
});

// ---- the step ----

test('the step installs the configured stack template as the project Dockerfile', () => {
  const root = makeRoot();
  const result = install(root);
  assert.equal(result.status, 0, result.stderr);
  const installed = fs.readFileSync(path.join(root, 'projects', 'acceptance-project', 'Dockerfile'), 'utf8');
  const template = fs.readFileSync(path.join(REPO_ROOT, 'Docker Templates', 'Dockerfile-node.template'), 'utf8');
  assert.equal(installed, template);
});

test('an existing Dockerfile is left exactly as it was', () => {
  const root = makeRoot({ existingDockerfile: 'FROM node:22-alpine\n# customised by hand\n' });
  const result = install(root);
  assert.equal(result.status, 0, result.stderr);
  assert.equal(
    fs.readFileSync(path.join(root, 'projects', 'acceptance-project', 'Dockerfile'), 'utf8'),
    'FROM node:22-alpine\n# customised by hand\n'
  );
  assert.match(result.stdout, /already exists/);
});

test('running the step twice is a no-op the second time', () => {
  const root = makeRoot();
  assert.equal(install(root).status, 0);
  const first = fs.readFileSync(path.join(root, 'projects', 'acceptance-project', 'Dockerfile'), 'utf8');
  assert.equal(install(root).status, 0);
  assert.equal(fs.readFileSync(path.join(root, 'projects', 'acceptance-project', 'Dockerfile'), 'utf8'), first);
});

test('a missing project directory stops the step instead of creating one', () => {
  const root = makeRoot({ projectDir: false });
  const result = install(root);
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /does not exist/);
  assert.equal(fs.existsSync(path.join(root, 'projects')), false);
});

test('the templates themselves are never modified', () => {
  const root = makeRoot();
  const before = fs.readFileSync(path.join(root, 'Docker Templates', 'Dockerfile-node.template'), 'utf8');
  assert.equal(install(root).status, 0);
  assert.equal(fs.readFileSync(path.join(root, 'Docker Templates', 'Dockerfile-node.template'), 'utf8'), before);
});
