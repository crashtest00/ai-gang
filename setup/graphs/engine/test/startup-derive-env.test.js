'use strict';

// scripts/startup/derive-env.sh, driven for real against a temporary
// checkout: the step that turns the single platform .env an operator
// edits into each service's own environment file.
//
// The value that matters most here is AI_GANG_HOME. ScrumMaster's compose
// file resolves /projects and /agent-docs from
// ${AI_GANG_HOME:-$HOME/ai-gang}, and the daemon evaluates that on the
// host; left unset, the daemon creates two empty directories and
// ScrumMaster comes up healthy and blind.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const REPO_ROOT = path.join(__dirname, '..', '..', '..', '..');
const DERIVE_ENV = path.join(REPO_ROOT, 'scripts', 'startup', 'derive-env.sh');
const ENV_TEMPLATE = path.join(REPO_ROOT, '.env.template');
const REAL_SM_ENV_EXAMPLE = path.join(REPO_ROOT, 'services', 'scrummaster', '.env.example');

const PLATFORM_ENV = [
  'ANTHROPIC_API_KEY=sk-ant-test-not-a-real-key',
  'GH_TOKEN=github_pat_test_not_a_real_token',
  'AIGANG_ADMIN_USER=operator',
  'AIGANG_ADMIN_EMAIL=operator@example.invalid',
  'AIGANG_ADMIN_PASSWORD=a-test-only-password',
  'PGPASSWORD=a-test-only-pg-password',
  'DJANGO_SECRET_KEY=a-test-only-django-key',
].join('\n') + '\n';

function makeRoot({ platformEnv = PLATFORM_ENV, scrummasterEnv = null } = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'aigang-derive-env-'));
  fs.mkdirSync(path.join(root, 'services', 'work-item-service'), { recursive: true });
  fs.mkdirSync(path.join(root, 'services', 'scrummaster'), { recursive: true });
  fs.writeFileSync(path.join(root, '.env'), platformEnv);
  fs.copyFileSync(ENV_TEMPLATE, path.join(root, '.env.template'));
  fs.copyFileSync(REAL_SM_ENV_EXAMPLE, path.join(root, 'services', 'scrummaster', '.env.example'));
  if (scrummasterEnv !== null) {
    fs.writeFileSync(path.join(root, 'services', 'scrummaster', '.env'), scrummasterEnv);
  }
  return root;
}

function run(root) {
  return spawnSync('bash', [DERIVE_ENV], {
    encoding: 'utf8',
    timeout: 20000,
    env: { ...process.env, AIGANG_ROOT: root, AIGANG_STATE_DIR: path.join(root, '.ai-gang') },
  });
}

function parseEnvFile(file) {
  const out = {};
  for (const line of fs.readFileSync(file, 'utf8').split('\n')) {
    if (!line || line.trimStart().startsWith('#')) continue;
    const i = line.indexOf('=');
    if (i === -1) continue;
    out[line.slice(0, i).trim()] = line.slice(i + 1);
  }
  return out;
}

test("the work-item service's environment file carries its database, Redis and Django settings", () => {
  const root = makeRoot();
  const result = run(root);
  assert.equal(result.status, 0, result.stderr);

  const derived = parseEnvFile(path.join(root, 'services', 'work-item-service', '.env'));
  assert.equal(derived.PGPASSWORD, 'a-test-only-pg-password');
  assert.equal(derived.DJANGO_SECRET_KEY, 'a-test-only-django-key');
  // Not set in the platform .env, so these come from the contract's defaults.
  assert.equal(derived.PGUSER, 'workitem');
  assert.equal(derived.PGDATABASE, 'workitem');
  assert.equal(derived.PGHOST, 'workitem-postgres');
  assert.equal(derived.PGPORT, '5432');
  assert.equal(derived.REDIS_HOST, 'ai-gang-redis');
  assert.equal(derived.PORT, '9100');
});

test('an operator override in the platform .env wins over the contract default', () => {
  const root = makeRoot({ platformEnv: PLATFORM_ENV + 'PGUSER=chosen-role\nPGDATABASE=chosen-db\n' });
  assert.equal(run(root).status, 0);
  const derived = parseEnvFile(path.join(root, 'services', 'work-item-service', '.env'));
  assert.equal(derived.PGUSER, 'chosen-role');
  assert.equal(derived.PGDATABASE, 'chosen-db');
});

// BUGFIXES.md BF-01 Pass 1 audit row 1: workitems/jira_interpret.py and
// workitems/webhook_consumer.py read these JIRA_*_FIELD_ID variables via
// os.environ, but the documented startup flow never carried them from the
// platform .env into the work-item service's own .env — they always saw
// unconfigured ids. This is the full list jira_interpret.py and
// webhook_consumer.py read (Story fields, the five BF-01 Release fields,
// and the Agent/Blocked fields).
const ALL_JIRA_FIELD_ID_VARS = [
  'JIRA_AGENT_FIELD_ID', 'JIRA_BLOCKED_FIELD_ID',
  'JIRA_VALUE_HYPOTHESIS_FIELD_ID', 'JIRA_TEST_MEASUREMENT_FIELD_ID', 'JIRA_BEHAVIOR_FIELD_ID',
  'JIRA_AC_FIELD_ID', 'JIRA_CONSTRAINTS_FIELD_ID', 'JIRA_EDGE_CASES_FIELD_ID', 'JIRA_OUT_OF_SCOPE_FIELD_ID',
  'JIRA_TARGET_PROJECT_FIELD_ID', 'JIRA_RELEASE_NOTES_FIELD_ID', 'JIRA_CANDIDATE_SHA_FIELD_ID',
  'JIRA_BUILD_IDENTIFIER_FIELD_ID', 'JIRA_PREVIEW_URL_FIELD_ID',
];

test('a JIRA_*_FIELD_ID set in the platform .env is carried into the work-item service .env, and an unset one is left out entirely', () => {
  const root = makeRoot({
    platformEnv: PLATFORM_ENV + 'JIRA_BEHAVIOR_FIELD_ID=customfield_10050\nJIRA_TARGET_PROJECT_FIELD_ID=customfield_10060\n',
  });
  assert.equal(run(root).status, 0);
  const derived = parseEnvFile(path.join(root, 'services', 'work-item-service', '.env'));
  assert.equal(derived.JIRA_BEHAVIOR_FIELD_ID, 'customfield_10050');
  assert.equal(derived.JIRA_TARGET_PROJECT_FIELD_ID, 'customfield_10060');
  // Not set in the platform .env — a local-mode-only deployment must see
  // no trace of them, not even an empty assignment.
  for (const name of ALL_JIRA_FIELD_ID_VARS) {
    if (name === 'JIRA_BEHAVIOR_FIELD_ID' || name === 'JIRA_TARGET_PROJECT_FIELD_ID') continue;
    assert.equal(name in derived, false, `${name} should not appear when unset in the platform .env`);
  }
});

test('every JIRA_*_FIELD_ID the work-item service reads is wired through when the platform .env sets it', () => {
  const extra = ALL_JIRA_FIELD_ID_VARS.map((name, i) => `${name}=customfield_${10000 + i}`).join('\n') + '\n';
  const root = makeRoot({ platformEnv: PLATFORM_ENV + extra });
  assert.equal(run(root).status, 0);
  const derived = parseEnvFile(path.join(root, 'services', 'work-item-service', '.env'));
  ALL_JIRA_FIELD_ID_VARS.forEach((name, i) => {
    assert.equal(derived[name], `customfield_${10000 + i}`, `${name} was not carried into the work-item service .env`);
  });
});

test("ScrumMaster's environment file gets AI_GANG_HOME set to the checkout's own path", () => {
  const root = makeRoot();
  assert.equal(run(root).status, 0);
  const derived = parseEnvFile(path.join(root, 'services', 'scrummaster', '.env'));
  assert.equal(derived.AI_GANG_HOME, root);
  assert.equal(derived.REDIS_HOST, 'ai-gang-redis');
  assert.equal(derived.PORT, '9000');
});

test("an absent ScrumMaster .env is seeded from the service's own .env.example", () => {
  const root = makeRoot();
  assert.equal(run(root).status, 0);
  const text = fs.readFileSync(path.join(root, 'services', 'scrummaster', '.env'), 'utf8');
  assert.match(text, /JIRA_AGENT_FIELD_ID=/, 'the example file\'s own keys should still be there');
});

test('an existing ScrumMaster .env keeps every value this flow does not own', () => {
  // scripts/create-jira-fields.sh and scripts/init-project.sh both write
  // into this file. Derivation must never be the thing that loses them.
  const existing = [
    'JIRA_AGENT_FIELD_ID=customfield_10099',
    'WEBHOOK_SECRET=an-existing-secret',
    'AI_GANG_HOME=/somewhere/stale',
    'REDIS_HOST=stale-host',
  ].join('\n') + '\n';
  const root = makeRoot({ scrummasterEnv: existing });
  assert.equal(run(root).status, 0);
  const derived = parseEnvFile(path.join(root, 'services', 'scrummaster', '.env'));
  assert.equal(derived.JIRA_AGENT_FIELD_ID, 'customfield_10099');
  assert.equal(derived.WEBHOOK_SECRET, 'an-existing-secret');
  assert.equal(derived.AI_GANG_HOME, root, 'a stale AI_GANG_HOME must be corrected');
  assert.equal(derived.REDIS_HOST, 'ai-gang-redis');
});

test('running twice leaves exactly the same files, with no duplicated assignments', () => {
  const root = makeRoot();
  assert.equal(run(root).status, 0);
  const first = {
    wis: fs.readFileSync(path.join(root, 'services', 'work-item-service', '.env'), 'utf8'),
    sm: fs.readFileSync(path.join(root, 'services', 'scrummaster', '.env'), 'utf8'),
  };
  assert.equal(run(root).status, 0);
  const second = {
    wis: fs.readFileSync(path.join(root, 'services', 'work-item-service', '.env'), 'utf8'),
    sm: fs.readFileSync(path.join(root, 'services', 'scrummaster', '.env'), 'utf8'),
  };
  assert.equal(second.wis, first.wis);
  assert.equal(second.sm, first.sm);
  const homeAssignments = second.sm.split('\n').filter((l) => l.startsWith('AI_GANG_HOME='));
  assert.equal(homeAssignments.length, 1);
});

test('both derived files are written owner-readable only', () => {
  const root = makeRoot();
  assert.equal(run(root).status, 0);
  for (const file of [
    path.join(root, 'services', 'work-item-service', '.env'),
    path.join(root, 'services', 'scrummaster', '.env'),
  ]) {
    assert.equal(fs.statSync(file).mode & 0o077, 0, `${file} must not be group- or world-readable`);
  }
});

test('no secret reaches the step output', () => {
  const root = makeRoot();
  const result = run(root);
  const output = `${result.stdout}${result.stderr}`;
  for (const secret of ['a-test-only-pg-password', 'a-test-only-django-key', 'github_pat_test_not_a_real_token']) {
    assert.equal(output.includes(secret), false);
  }
});

test('a quoted value in the platform .env is unquoted the way Compose unquotes it', () => {
  const root = makeRoot({ platformEnv: PLATFORM_ENV.replace('PGPASSWORD=a-test-only-pg-password', 'PGPASSWORD="a quoted password"') });
  assert.equal(run(root).status, 0);
  const derived = parseEnvFile(path.join(root, 'services', 'work-item-service', '.env'));
  assert.equal(derived.PGPASSWORD, 'a quoted password');
});
