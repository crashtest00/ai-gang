'use strict';

// Unit-level coverage of the platform half of
// setup/graphs/engine/lib/config/validate.js: the `repository` object and
// the template-placeholder check that ai-gang.config.json is held to, and
// the guarantee that neither of them changed anything for a project
// configuration going through scripts/init-project.sh --config.
//
// The entrypoint that actually runs these rules is cli.js --platform (see
// config-cli-platform.test.js) and, above that, the AI Gang container's
// entrypoint. This file covers the shape cases exhaustively and quickly.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {
  validateConfigText,
  validatePlatformConfigFile,
} = require('../lib/config/validate');

const REPO_ROOT = path.join(__dirname, '..', '..', '..', '..');
const PLATFORM_TEMPLATE_PATH = path.join(REPO_ROOT, 'ai-gang.config.template.json');

// The platform rules have one entry point, `validatePlatformConfigFile` —
// the one cli.js --platform calls. These cases are about the text, so
// they write it where an operator's ai-gang.config.json would be and go
// in through that same door.
function validatePlatformConfigText(text) {
  const file = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'aigang-platform-config-')), 'ai-gang.config.json');
  fs.writeFileSync(file, text);
  return validatePlatformConfigFile(file);
}

function valid(overrides = {}) {
  return {
    schemaVersion: 1,
    project: { name: 'acceptance-project', type: 'web', stack: 'node-express' },
    repository: { url: 'https://github.com/an-org/a-repo.git' },
    ...overrides,
  };
}

// ---- the shipped template ----

test('the template ships at the repository root, where an operator copies it from', () => {
  assert.ok(fs.existsSync(PLATFORM_TEMPLATE_PATH), `${PLATFORM_TEMPLATE_PATH} must exist`);
});

test('the shipped template is itself valid JSON with the expected shape', () => {
  const doc = JSON.parse(fs.readFileSync(PLATFORM_TEMPLATE_PATH, 'utf8'));
  assert.equal(doc.schemaVersion, 1);
  assert.deepEqual(Object.keys(doc).sort(), ['project', 'repository', 'schemaVersion']);
  assert.deepEqual(Object.keys(doc.project).sort(), ['name', 'stack', 'type']);
  assert.deepEqual(Object.keys(doc.repository), ['url']);
});

test('the unedited template is rejected, naming every field left unfilled', () => {
  const result = validatePlatformConfigFile(PLATFORM_TEMPLATE_PATH);
  assert.equal(result.valid, false);
  for (const field of ['project.name', 'project.type', 'project.stack', 'repository.url']) {
    assert.ok(
      result.errors.some((e) => e.includes(`"${field}"`) && /placeholder/.test(e)),
      `expected a placeholder diagnostic naming ${field}, got: ${result.errors.join(' | ')}`
    );
  }
});

test('a placeholder diagnostic for a catalog-backed field lists the supported values', () => {
  const doc = JSON.parse(fs.readFileSync(PLATFORM_TEMPLATE_PATH, 'utf8'));
  const result = validatePlatformConfigText(JSON.stringify(doc));
  const typeError = result.errors.find((e) => e.includes('"project.type"'));
  assert.match(typeError, /web/);
});

test('one field left at its placeholder is rejected even when the rest are filled in', () => {
  const doc = JSON.parse(fs.readFileSync(PLATFORM_TEMPLATE_PATH, 'utf8'));
  const config = valid();
  config.repository.url = doc.repository.url;
  const result = validatePlatformConfigText(JSON.stringify(config));
  assert.equal(result.valid, false);
  assert.equal(result.errors.length, 1);
  assert.match(result.errors[0], /"repository\.url" is still at its ai-gang\.config\.template\.json placeholder/);
});

// ---- the repository object ----

test('a complete platform configuration validates and yields the repository URL', () => {
  const result = validatePlatformConfigText(JSON.stringify(valid()));
  assert.equal(result.valid, true);
  assert.deepEqual(result.decisions, {
    name: 'acceptance-project',
    type: 'web',
    stack: 'node-express',
    repositoryUrl: 'https://github.com/an-org/a-repo.git',
  });
});

test('a platform configuration without a repository object is rejected', () => {
  const config = valid();
  delete config.repository;
  const result = validatePlatformConfigText(JSON.stringify(config));
  assert.equal(result.valid, false);
  assert.ok(result.errors.some((e) => /missing required field "repository"/.test(e)));
});

test('an unknown field inside repository is rejected', () => {
  const result = validatePlatformConfigText(
    JSON.stringify(valid({ repository: { url: 'https://github.com/a/b.git', branch: 'main' } }))
  );
  assert.equal(result.valid, false);
  assert.ok(result.errors.some((e) => /unknown field "repository\.branch"/.test(e)));
});

test('a repository that is not an object is rejected', () => {
  for (const value of ['https://github.com/a/b.git', 42, null, []]) {
    const result = validatePlatformConfigText(JSON.stringify(valid({ repository: value })));
    assert.equal(result.valid, false, `expected ${JSON.stringify(value)} to be rejected`);
    assert.ok(result.errors.some((e) => /"repository" must be an object|missing required field "repository"/.test(e)));
  }
});

test('a missing or empty repository.url is rejected by name', () => {
  for (const repository of [{}, { url: '' }]) {
    const result = validatePlatformConfigText(JSON.stringify(valid({ repository })));
    assert.equal(result.valid, false);
    assert.ok(result.errors.some((e) => /"repository\.url"/.test(e)));
  }
});

test('a repository.url carrying whitespace or a newline is rejected', () => {
  // The URL leaves the validator as a KEY=value line a shell reads back,
  // so a value that could break that framing must never validate.
  for (const url of ['https://github.com/a/b.git\nPROJECT_NAME=evil', 'https://github.com/a b.git', ' https://x']) {
    const result = validatePlatformConfigText(JSON.stringify(valid({ repository: { url } })));
    assert.equal(result.valid, false, `expected ${JSON.stringify(url)} to be rejected`);
    assert.ok(result.errors.some((e) => /printable, non-space ASCII/.test(e)));
  }
});

// ---- the project path is unchanged ----

test('a project configuration is still valid with no repository object at all', () => {
  const config = valid();
  delete config.repository;
  const result = validateConfigText(JSON.stringify(config));
  assert.equal(result.valid, true);
  assert.deepEqual(result.decisions, { name: 'acceptance-project', type: 'web', stack: 'node-express' });
});

test('a project configuration may carry a repository object, and then yields its URL too', () => {
  const result = validateConfigText(JSON.stringify(valid()));
  assert.equal(result.valid, true);
  assert.equal(result.decisions.repositoryUrl, 'https://github.com/an-org/a-repo.git');
});

test('the placeholder check does not apply to a project configuration', () => {
  const doc = JSON.parse(fs.readFileSync(PLATFORM_TEMPLATE_PATH, 'utf8'));
  const config = valid();
  config.repository.url = doc.repository.url;
  const result = validateConfigText(JSON.stringify(config));
  assert.equal(result.valid, true, result.errors.join(' | '));
});

test('the project field rules and the catalog lookup are unchanged on both paths', () => {
  const cases = [
    [valid({ project: { name: 'Bad Name', type: 'web', stack: 'node-express' } }), /not a safe project name/],
    [valid({ project: { name: 'ok-name', type: 'mobile', stack: 'node-express' } }), /not a supported deployment target/],
    [valid({ project: { name: 'ok-name', type: 'web', stack: 'nope' } }), /not a supported stack profile/],
    [valid({ project: { name: 'ok-name', type: 'web' } }), /missing required field "project\.stack"/],
    [valid({ extra: true }), /unknown field "extra"/],
  ];
  for (const [config, pattern] of cases) {
    const text = JSON.stringify(config);
    for (const [label, validate] of [['project', validateConfigText], ['platform', validatePlatformConfigText]]) {
      const result = validate(text);
      assert.equal(result.valid, false, `${label}: expected ${text} to be rejected`);
      assert.ok(
        result.errors.some((e) => pattern.test(e)),
        `${label}: expected ${pattern} in ${result.errors.join(' | ')}`
      );
    }
  }
});
