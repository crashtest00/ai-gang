'use strict';

// The platform .env contract, driven through the real scripts:
// scripts/startup/env-contract.sh (the list), scripts/startup/validate-env.sh
// (what the AI Gang container's entrypoint runs before any service
// container can exist), and .env.template (what an operator copies).
//
// The three are checked against each other here, not against a restated
// copy of the list, so a variable cannot be added to one and forgotten in
// the others.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const REPO_ROOT = path.join(__dirname, '..', '..', '..', '..');
const CONTRACT = path.join(REPO_ROOT, 'scripts', 'startup', 'env-contract.sh');
const VALIDATE_ENV = path.join(REPO_ROOT, 'scripts', 'startup', 'validate-env.sh');
const ENV_TEMPLATE = path.join(REPO_ROOT, '.env.template');

function readContract() {
  const result = spawnSync('bash', [CONTRACT], { encoding: 'utf8', timeout: 15000 });
  assert.equal(result.status, 0, result.stderr);
  return result.stdout
    .trim()
    .split('\n')
    .map((line) => {
      const [kind, name, ...rest] = line.split(' ');
      return { kind, name, default: rest.join(' ') };
    });
}

// The template's assignment for a variable, or undefined if it has none.
function templateValue(name) {
  const text = fs.readFileSync(ENV_TEMPLATE, 'utf8');
  const line = text.split('\n').filter((l) => new RegExp(`^\\s*${name}=`).test(l)).pop();
  return line === undefined ? undefined : line.slice(line.indexOf('=') + 1);
}

// A .env holding a valid value for every contract variable.
const FILLED = {
  ANTHROPIC_API_KEY: 'sk-ant-test-not-a-real-key',
  GH_TOKEN: 'github_pat_test_not_a_real_token',
  AIGANG_ADMIN_USER: 'operator',
  AIGANG_ADMIN_EMAIL: 'operator@example.invalid',
  AIGANG_ADMIN_PASSWORD: 'a-test-only-password',
  PGPASSWORD: 'a-test-only-pg-password',
  DJANGO_SECRET_KEY: 'a-test-only-django-key',
};

function makeRoot(envOverrides = {}, { omit = [] } = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'aigang-startup-env-'));
  const values = { ...FILLED, ...envOverrides };
  const lines = Object.entries(values)
    .filter(([name]) => !omit.includes(name))
    .map(([name, value]) => `${name}=${value}`);
  fs.writeFileSync(path.join(root, '.env'), lines.join('\n') + '\n');
  fs.copyFileSync(ENV_TEMPLATE, path.join(root, '.env.template'));
  return root;
}

function runValidateEnv(root) {
  return spawnSync('bash', [VALIDATE_ENV], {
    encoding: 'utf8',
    timeout: 20000,
    env: {
      ...process.env,
      AIGANG_ROOT: root,
      AIGANG_STATE_DIR: path.join(root, 'nonexistent-state'),
    },
  });
}

// ---- the contract, .env.template, and each other ----

test('the contract is well formed: REQUIRED or OPTIONAL, one variable each', () => {
  const entries = readContract();
  assert.ok(entries.length > 0);
  for (const entry of entries) {
    assert.ok(['REQUIRED', 'OPTIONAL'].includes(entry.kind), `bad kind: ${entry.kind}`);
    assert.match(entry.name, /^[A-Z][A-Z0-9_]*$/);
    if (entry.kind === 'OPTIONAL') assert.notEqual(entry.default, '');
  }
  const names = entries.map((e) => e.name);
  assert.equal(new Set(names).size, names.length, 'a variable is listed twice');
});

test('every contract variable is declared in .env.template', () => {
  for (const entry of readContract()) {
    assert.notEqual(
      templateValue(entry.name),
      undefined,
      `${entry.name} is in the contract but not declared in .env.template`
    );
  }
});

test('every REQUIRED variable ships empty in .env.template, so an unfilled copy fails', () => {
  for (const entry of readContract().filter((e) => e.kind === 'REQUIRED')) {
    assert.equal(
      templateValue(entry.name),
      '',
      `${entry.name} is REQUIRED and must ship empty in .env.template`
    );
  }
});

test("every OPTIONAL variable's default matches what .env.template ships", () => {
  for (const entry of readContract().filter((e) => e.kind === 'OPTIONAL')) {
    assert.equal(
      templateValue(entry.name),
      entry.default,
      `${entry.name}'s .env.template value and its contract default disagree`
    );
  }
});

// ---- validate-env.sh ----

test('a complete .env passes', () => {
  const result = runValidateEnv(makeRoot());
  assert.equal(result.status, 0, result.stderr);
});

test('each required variable, removed in turn, fails and is named', () => {
  for (const entry of readContract().filter((e) => e.kind === 'REQUIRED')) {
    const result = runValidateEnv(makeRoot({}, { omit: [entry.name] }));
    assert.notEqual(result.status, 0, `removing ${entry.name} should have failed`);
    assert.ok(
      result.stderr.includes(entry.name),
      `expected ${entry.name} to be named, got: ${result.stderr}`
    );
  }
});

test('each required variable, left empty, fails and is named', () => {
  for (const entry of readContract().filter((e) => e.kind === 'REQUIRED')) {
    const result = runValidateEnv(makeRoot({ [entry.name]: '' }));
    assert.notEqual(result.status, 0, `an empty ${entry.name} should have failed`);
    assert.ok(result.stderr.includes(entry.name));
  }
});

test('a required variable still holding the template value is refused as a placeholder', () => {
  // .env.template ships every required variable empty, so the placeholder
  // rule is exercised against a template that does not — proving the rule
  // is the comparison, not a coincidence of emptiness.
  const root = makeRoot({ AIGANG_ADMIN_EMAIL: 'you@example.com' });
  fs.writeFileSync(
    path.join(root, '.env.template'),
    fs.readFileSync(ENV_TEMPLATE, 'utf8').replace(/^AIGANG_ADMIN_EMAIL=$/m, 'AIGANG_ADMIN_EMAIL=you@example.com')
  );
  const result = runValidateEnv(root);
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /AIGANG_ADMIN_EMAIL is still at its \.env\.template placeholder/);
});

test('optional variables may be absent entirely', () => {
  const root = makeRoot();
  const result = runValidateEnv(root);
  assert.equal(result.status, 0, result.stderr);
  const written = fs.readFileSync(path.join(root, '.env'), 'utf8');
  assert.equal(written.includes('PGUSER='), false, 'the fixture deliberately omits the optional variables');
});

test('a missing .env is reported, naming the file', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'aigang-startup-env-empty-'));
  const result = runValidateEnv(root);
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /no environment file at/);
});

test('no secret value is echoed by a passing or failing run', () => {
  const secrets = [FILLED.ANTHROPIC_API_KEY, FILLED.GH_TOKEN, FILLED.AIGANG_ADMIN_PASSWORD,
    FILLED.PGPASSWORD, FILLED.DJANGO_SECRET_KEY];
  for (const root of [makeRoot(), makeRoot({}, { omit: ['GH_TOKEN'] })]) {
    const result = runValidateEnv(root);
    const output = `${result.stdout}${result.stderr}`;
    for (const secret of secrets) {
      assert.equal(output.includes(secret), false, `a secret value reached the output: ${output}`);
    }
  }
});
