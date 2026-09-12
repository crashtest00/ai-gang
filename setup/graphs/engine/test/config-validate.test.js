'use strict';

// Unit-level coverage of setup/graphs/engine/lib/config/validate.js — the
// shared validation path setup/graphs/engine/lib/config/cli.js calls, which
// in turn is what scripts/init-project.sh --config invokes (see
// config-init-cli.test.js for the same requirements exercised through that
// real, end-to-end entrypoint). This file covers structural/shape edge
// cases exhaustively and quickly, without a subprocess per case.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { validateConfigFile } = require('../lib/config/validate');

const INIT_PROJECT_SCRIPT = path.join(__dirname, '..', '..', '..', '..', 'scripts', 'init-project.sh');

// The project rules have one entry point, `validateConfigFile` — the one
// cli.js calls without --platform. These cases are about the text, so they
// write it where a project config file would be and go in through that
// same door (the same technique config-validate-platform.test.js uses for
// the platform rules).
function validateConfigText(text) {
  const file = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'aigang-project-config-')), 'config.json');
  fs.writeFileSync(file, text);
  return validateConfigFile(file);
}

function validExample(overrides = {}) {
  return {
    schemaVersion: 1,
    project: { name: 'acceptance-project', type: 'web', stack: 'node-express' },
    ...overrides,
  };
}

test('validateConfigText: accepts the shipped valid example', () => {
  const result = validateConfigText(JSON.stringify(validExample()));
  assert.equal(result.valid, true);
  assert.deepEqual(result.errors, []);
  assert.deepEqual(result.decisions, { name: 'acceptance-project', type: 'web', stack: 'node-express' });
});

test('validateConfigText: rejects malformed JSON', () => {
  const result = validateConfigText('{not json');
  assert.equal(result.valid, false);
  assert.ok(result.errors.some((e) => /not valid JSON/.test(e)));
});

test('validateConfigText: rejects a duplicate key even when the duplicate value is itself harmless', () => {
  const text = '{"schemaVersion":1,"schemaVersion":1,"project":{"name":"a","type":"web","stack":"node-express"}}';
  const result = validateConfigText(text);
  assert.equal(result.valid, false);
  assert.ok(result.errors.some((e) => /duplicate key "schemaVersion"/.test(e)));
});

test('validateConfigText: rejects an unknown top-level field', () => {
  const result = validateConfigText(JSON.stringify({ ...validExample(), extra: true }));
  assert.equal(result.valid, false);
  assert.ok(result.errors.some((e) => /unknown field "extra"/.test(e)));
});

test('validateConfigText: rejects an unknown project field', () => {
  const result = validateConfigText(
    JSON.stringify(validExample({ project: { name: 'a', type: 'web', stack: 'node-express', extra: 1 } }))
  );
  assert.equal(result.valid, false);
  assert.ok(result.errors.some((e) => /unknown field "project.extra"/.test(e)));
});

test('validateConfigText: rejects an unsupported schemaVersion', () => {
  const result = validateConfigText(JSON.stringify(validExample({ schemaVersion: 2 })));
  assert.equal(result.valid, false);
  assert.ok(result.errors.some((e) => /unsupported schemaVersion/.test(e)));
});

test('validateConfigText: rejects a schemaVersion of the wrong type', () => {
  const result = validateConfigText(JSON.stringify(validExample({ schemaVersion: '1' })));
  assert.equal(result.valid, false);
  assert.ok(result.errors.some((e) => /unsupported schemaVersion/.test(e)));
});

test('validateConfigText: rejects a missing schemaVersion', () => {
  const doc = validExample();
  delete doc.schemaVersion;
  const result = validateConfigText(JSON.stringify(doc));
  assert.equal(result.valid, false);
  assert.ok(result.errors.some((e) => /missing required field "schemaVersion"/.test(e)));
});

test('validateConfigText: rejects a missing "project" object', () => {
  const result = validateConfigText(JSON.stringify({ schemaVersion: 1 }));
  assert.equal(result.valid, false);
  assert.ok(result.errors.some((e) => /missing required field "project"/.test(e)));
});

test('validateConfigText: rejects "project" as the wrong type', () => {
  const result = validateConfigText(JSON.stringify({ schemaVersion: 1, project: 'web' }));
  assert.equal(result.valid, false);
  assert.ok(result.errors.some((e) => /"project" must be an object/.test(e)));
});

for (const field of ['name', 'type', 'stack']) {
  test(`validateConfigText: rejects a missing project.${field}`, () => {
    const doc = validExample();
    delete doc.project[field];
    const result = validateConfigText(JSON.stringify(doc));
    assert.equal(result.valid, false);
    assert.ok(result.errors.some((e) => e.includes(`missing required field "project.${field}"`)));
  });

  test(`validateConfigText: rejects project.${field} of the wrong type`, () => {
    const result = validateConfigText(JSON.stringify(validExample({ project: { ...validExample().project, [field]: 42 } })));
    assert.equal(result.valid, false);
    assert.ok(result.errors.some((e) => e.includes(`"project.${field}" must be a nonempty string`)));
  });

  test(`validateConfigText: rejects an empty-string project.${field}`, () => {
    const result = validateConfigText(JSON.stringify(validExample({ project: { ...validExample().project, [field]: '' } })));
    assert.equal(result.valid, false);
    assert.ok(result.errors.some((e) => e.includes(`"project.${field}" must be a nonempty string`)));
  });
}

test('validateConfigText: rejects an unsupported project.type, listing supported targets', () => {
  const result = validateConfigText(JSON.stringify(validExample({ project: { name: 'a', type: 'spaceship', stack: 'node-express' } })));
  assert.equal(result.valid, false);
  assert.ok(result.errors.some((e) => /"project.type" "spaceship" is not a supported deployment target/.test(e)));
  assert.ok(result.errors.some((e) => e.includes('web')));
});

test('validateConfigText: rejects an unsupported project.stack, listing supported stacks for that target', () => {
  const result = validateConfigText(JSON.stringify(validExample({ project: { name: 'a', type: 'web', stack: 'rust-actix' } })));
  assert.equal(result.valid, false);
  assert.ok(result.errors.some((e) => /"project.stack" "rust-actix" is not a supported stack profile for target "web"/.test(e)));
  assert.ok(result.errors.some((e) => e.includes('node-express')));
});

test('validateConfigText: an incompatible target/stack pair (both individually plausible-looking) is rejected', () => {
  // "python-flask" reads like a real stack identifier — just not one
  // supported for "web" today.
  const result = validateConfigText(JSON.stringify(validExample({ project: { name: 'a', type: 'web', stack: 'python-flask' } })));
  assert.equal(result.valid, false);
  assert.ok(result.errors.some((e) => /"project.stack" "python-flask" is not a supported stack profile/.test(e)));
});

test('validateConfigText: rejects an unsafe project name (uppercase)', () => {
  const result = validateConfigText(JSON.stringify(validExample({ project: { name: 'Acceptance-Project', type: 'web', stack: 'node-express' } })));
  assert.equal(result.valid, false);
  assert.ok(result.errors.some((e) => /not a safe project name/.test(e)));
});

test('validateConfigText: rejects an unsafe project name (starts with a digit)', () => {
  const result = validateConfigText(JSON.stringify(validExample({ project: { name: '1-project', type: 'web', stack: 'node-express' } })));
  assert.equal(result.valid, false);
  assert.ok(result.errors.some((e) => /not a safe project name/.test(e)));
});

test('validateConfigText: rejects a project name containing shell metacharacters', () => {
  const result = validateConfigText(JSON.stringify(validExample({ project: { name: 'a; touch x', type: 'web', stack: 'node-express' } })));
  assert.equal(result.valid, false);
  assert.ok(result.errors.some((e) => /not a safe project name/.test(e)));
});

// scripts/init-project.sh validates a typed-in project name with
// `grep -qE '<pattern>'`; extract that same pattern straight from the
// script source, instead of hardcoding a second copy here that could
// silently drift, and run the identical grep check against a candidate
// name.
function interactiveFlowAccepts(name) {
  const scriptText = fs.readFileSync(INIT_PROJECT_SCRIPT, 'utf8');
  const match = scriptText.match(/grep -qE '(\^\[a-z\][^']+)'/);
  assert.ok(match, 'expected to find the interactive project-name grep pattern in scripts/init-project.sh');
  const pattern = match[1];
  const result = spawnSync('bash', ['-c', `echo "$1" | grep -qE '${pattern}'`, '--', name]);
  return result.status === 0;
}

function configFlowAccepts(name) {
  return validateConfigText(JSON.stringify(validExample({ project: { ...validExample().project, name } }))).valid;
}

test('the interactive-flow project-name rule and the config validator accept and reject the same sample names', () => {
  const samples = [
    'acceptance-project', // valid
    'a1', // valid, minimum length
    'a-b-c', // valid, hyphenated
    'a', // invalid, too short
    'Acceptance-Project', // invalid, uppercase
    '1-project', // invalid, starts with a digit
    'a; touch x', // invalid, shell metacharacters
    '_project', // invalid, starts with underscore
  ];

  for (const name of samples) {
    assert.equal(
      configFlowAccepts(name),
      interactiveFlowAccepts(name),
      `expected the interactive flow and the config validator to agree on "${name}"`
    );
  }
});
