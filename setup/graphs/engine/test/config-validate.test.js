'use strict';

// Unit-level coverage of setup/graphs/engine/lib/config/validate.js — the
// shared validation path setup/graphs/engine/lib/config/cli.js calls, which
// in turn is what scripts/init-project.sh --config invokes (see
// config-init-cli.test.js for the same requirements exercised through that
// real, end-to-end entrypoint). This file covers structural/shape edge
// cases exhaustively and quickly, without a subprocess per case.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const { validateConfigText, PROJECT_NAME_PATTERN } = require('../lib/config/validate');

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

test('PROJECT_NAME_PATTERN matches the interactive-flow rule in scripts/init-project.sh', () => {
  // scripts/init-project.sh checks `grep -qE '^[a-z][a-z0-9-]+$'` — same
  // pattern, so a config-driven name is held to the identical rule.
  assert.equal(PROJECT_NAME_PATTERN.source, '^[a-z][a-z0-9-]+$');
});
