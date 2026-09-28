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
const DERIVE_ENV = path.join(REPO_ROOT, 'scripts', 'startup', 'derive-env.sh');

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

// ---- the variables the contract deliberately does not list ----

// Every variable derive-env.sh copies out of the platform .env by name, rather
// than through the contract's env_value: the `env_file_get "$AIGANG_ENV_FILE"
// <NAME>` calls, plus the loop over JIRA_FIELD_ID_VARS. Read out of the script
// so this is the real set, not a restated copy of it.
function copiedThroughVariables() {
  const text = fs.readFileSync(DERIVE_ENV, 'utf8');
  const names = new Set();
  for (const m of text.matchAll(/env_file_get "\$AIGANG_ENV_FILE" ([A-Z][A-Z0-9_]*)/g)) {
    names.add(m[1]);
  }
  const list = text.match(/JIRA_FIELD_ID_VARS=\(([^)]*)\)/);
  assert.ok(list, 'JIRA_FIELD_ID_VARS is no longer an array literal in derive-env.sh');
  for (const name of list[1].match(/[A-Z][A-Z0-9_]*/g) || []) names.add(name);
  assert.ok(names.size > 1, 'found no copied-through variables, so this check would be vacuous');
  return [...names].sort();
}

test('the copied-through variables are out of the contract on purpose and declared blank in .env.template', () => {
  // env-contract.sh's header scopes "the single list" to the variables startup
  // requires or defaults, and says these are defined at the place that copies
  // them instead. That is what this pins: startup neither requires nor
  // defaults them, so they have no contract entry — and .env.template still
  // declares each one, blank, so an operator can see it exists (V5.0 audit row
  // 87).
  const contractNames = new Set(readContract().map((e) => e.name));
  for (const name of copiedThroughVariables()) {
    assert.ok(
      !contractNames.has(name),
      `${name} is copied through by derive-env.sh and is now also in the contract — ` +
      'either give it a REQUIRED/OPTIONAL entry and stop copying it by name, or keep it out of both'
    );
    assert.equal(
      templateValue(name),
      '',
      `${name} is copied through by derive-env.sh and must ship declared-but-empty in .env.template`
    );
  }
});

test('a copied-through variable is omitted from the derived .env when the platform .env leaves it blank', () => {
  // The property that makes a contract entry wrong for these: unset is a
  // meaningful state, not a missing value to default.
  const text = fs.readFileSync(DERIVE_ENV, 'utf8');
  assert.match(text, /if webhook_secret="\$\(env_file_get "\$AIGANG_ENV_FILE" WEBHOOK_SECRET\)" && \[\[ -n "\$webhook_secret" \]\]; then/);
  assert.match(text, /if jira_value="\$\(env_file_get "\$AIGANG_ENV_FILE" "\$jira_var"\)" && \[\[ -n "\$jira_value" \]\]; then/);
});

// ---- the reverse direction: .env.template -> the contract ----

// Every name .env.template assigns, in source order — the set the two
// checks above build out of the contract and derive-env.sh only ever walk
// forward from, never back over. Read out of the file itself, same as
// templateValue reads a single one, so this is the real set too.
function templateAssignedVariables() {
  const text = fs.readFileSync(ENV_TEMPLATE, 'utf8');
  const names = new Set();
  for (const m of text.matchAll(/^([A-Z][A-Z0-9_]*)=/gm)) names.add(m[1]);
  return [...names].sort();
}

// Declared in .env.template but read by neither env-contract.sh nor
// derive-env.sh — confirmed by grepping scripts/startup/ for each name and
// finding it nowhere. Each belongs to a phase .env.template's own "PLATFORM
// STARTUP" section says an operator adds later — Jira, Cloudflare, Jenkins,
// the Beta VM — and reads instead through that phase's own script, named
// below. A name does not belong on this list because it is inconvenient;
// it belongs here only once its one real reader is confirmed, the same way
// copiedThroughVariables() above is read out of derive-env.sh rather than
// asserted.
const DECLARED_FOR_A_LATER_PHASE = new Map([
  ['JIRA_URL', 'scripts/init-jenkins.sh'],
  ['JIRA_EMAIL', 'scripts/init-jenkins.sh'],
  ['JIRA_TOKEN', 'scripts/init-jenkins.sh'],
  ['GITHUB_TOKEN', 'scripts/init-jenkins.sh'],
  ['JENKINS_GITHUB_USER', 'scripts/init-project.sh'],
  ['CF_API_KEY', 'scripts/spike-cloudflare-tunnel-api.sh'],
  ['CF_ZONE_ID', 'scripts/setup-cloudflare-tunnel.sh'],
  ['CF_ACCOUNT_ID', 'scripts/spike-cloudflare-tunnel-api.sh'],
  ['HQ_SUBDOMAIN', 'scripts/setup-cloudflare-tunnel.sh'],
  ['JENKINS_SUBDOMAIN', 'scripts/setup-cloudflare-tunnel.sh'],
  ['PREVIEW_SUBDOMAIN', 'scripts/setup-cloudflare-tunnel.sh'],
  ['BETA_DOMAIN', 'scripts/setup-beta-vm.sh'],
  ['BETA_VM_HOST', 'scripts/setup-beta-vm.sh'],
  ['BETA_VM_ADMIN_USER', 'scripts/setup-beta-vm.sh'],
  ['BETA_VM_TRAEFIK_PORT', 'scripts/setup-beta-vm.sh'],
  ['HQ_URL', 'scripts/init-project.sh'],
  ['JENKINS_URL', 'scripts/init-jenkins.sh'],
  ['JENKINS_ADMIN_PASSWORD', 'scripts/init-jenkins.sh'],
  ['PREVIEW_DOMAIN', 'scripts/setup-beta-vm.sh'],
]);

test('every .env.template variable is accounted for: contract, copied-through, or a named later-phase reader', () => {
  // The two tests above start from the contract and from derive-env.sh and
  // check outward to .env.template; nothing starts from .env.template and
  // checks back. A variable added there — a REQUIRED/OPTIONAL-shaped one
  // that never reached env-contract.sh, or a new JIRA_*_FIELD_ID that never
  // reached JIRA_FIELD_ID_VARS — would ship invisible to every check above,
  // read by nothing, silently inert. This closes that direction: every
  // assignment must land in the contract, in derive-env.sh's copied-through
  // set, or on the later-phase list, whose own reader is confirmed absent
  // from scripts/startup/ below. There is nowhere left for one to hide.
  const contractNames = new Set(readContract().map((e) => e.name));
  const copiedThrough = new Set(copiedThroughVariables());
  const startupDir = path.join(REPO_ROOT, 'scripts', 'startup');
  const startupText = fs.readdirSync(startupDir)
    .filter((f) => f.endsWith('.sh'))
    .map((f) => fs.readFileSync(path.join(startupDir, f), 'utf8'))
    .join('\n');
  for (const name of templateAssignedVariables()) {
    if (contractNames.has(name) || copiedThrough.has(name)) continue;
    assert.ok(
      DECLARED_FOR_A_LATER_PHASE.has(name),
      `${name} is in .env.template but in none of: the contract, derive-env.sh's copied-through ` +
      'set, or the later-phase list — give it a REQUIRED/OPTIONAL contract entry, wire it into ' +
      'derive-env.sh, or add it to DECLARED_FOR_A_LATER_PHASE with the script that reads it'
    );
    const reader = DECLARED_FOR_A_LATER_PHASE.get(name);
    assert.ok(
      new RegExp(`\\b${name}\\b`).test(fs.readFileSync(path.join(REPO_ROOT, reader), 'utf8')),
      `${name} is declared read by ${reader}, but that file no longer mentions it`
    );
    assert.ok(
      !new RegExp(`\\b${name}\\b`).test(startupText),
      `${name} is declared as a later-phase variable but scripts/startup/ reads it too — ` +
      'give it a contract entry or wire it into derive-env.sh instead'
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
