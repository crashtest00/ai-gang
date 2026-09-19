'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {
  listSupportedTargets,
  listSupportedStacks,
  isSupportedTarget,
  isSupportedStack,
} = require('../lib/config/catalog');

const TEMPLATES_WEB_DIR = path.join(__dirname, '..', '..', '..', '..', 'templates', 'web');

test('catalog: "web" is a supported target', () => {
  assert.ok(isSupportedTarget('web'));
  assert.ok(listSupportedTargets().includes('web'));
});

test('catalog: an unrecognized target is not supported', () => {
  assert.equal(isSupportedTarget('mobile'), false);
  assert.equal(isSupportedTarget('desktop'), false);
  assert.equal(isSupportedTarget('nonsense'), false);
});

test('catalog: "node-express" is the supported stack for "web"', () => {
  assert.ok(isSupportedStack('web', 'node-express'));
  assert.deepEqual(listSupportedStacks('web'), ['node-express']);
});

test('catalog: an unsupported stack for a supported target is rejected', () => {
  assert.equal(isSupportedStack('web', 'python-flask'), false);
});

test('catalog: a stack lookup against an unsupported target returns no stacks', () => {
  assert.deepEqual(listSupportedStacks('mobile'), []);
  assert.equal(isSupportedStack('mobile', 'node-express'), false);
});

// The catalog's one shipped web stack profile must actually describe the
// boilerplate that exists on disk — not a profile invented independently
// of what scripts/init-repo.sh --deployment web actually scaffolds.
test('catalog: the "node-express" profile matches the real templates/web/ boilerplate', () => {
  const pkg = JSON.parse(fs.readFileSync(path.join(TEMPLATES_WEB_DIR, 'package.json'), 'utf8'));
  assert.ok(pkg.dependencies && pkg.dependencies.express, 'templates/web/package.json must depend on express');
  assert.ok(fs.existsSync(path.join(TEMPLATES_WEB_DIR, 'server.js')));
});
