'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

process.env.AGENTS_CATALOG_PATH = path.join(__dirname, 'fixtures', 'agents.json');
process.env.PROJECTS_CONFIG_PATH = path.join(__dirname, 'fixtures', 'projects.json');

const jira = require('../src/jira');
const { diffAgentFieldOptions, auditAgentFieldDrift, scheduleAgentFieldAudit } = require('../src/audit');

// The environment a real Jira connection needs, and the exact placeholder
// values services/scrummaster/.env.example ships with — the ones an
// installation that never connected Jira still has in its derived .env.
const JIRA_ENV = ['JIRA_BASE_URL', 'JIRA_USER_EMAIL', 'JIRA_API_TOKEN', 'JIRA_AGENT_FIELD_ID'];
const CONFIGURED = {
  JIRA_BASE_URL: 'https://example.atlassian.net',
  JIRA_USER_EMAIL: 'bot@example.com',
  JIRA_API_TOKEN: 'a-token',
  JIRA_AGENT_FIELD_ID: 'customfield_10050',
};
const TEMPLATE_PLACEHOLDERS = {
  JIRA_BASE_URL: 'https://your-org.atlassian.net',
  JIRA_USER_EMAIL: 'ai-gang-bot@your-domain.com',
  JIRA_API_TOKEN: '',
  JIRA_AGENT_FIELD_ID: 'customfield_XXXXX',
};

function setJiraEnv(t, values) {
  const saved = Object.fromEntries(JIRA_ENV.map(name => [name, process.env[name]]));
  t.after(() => {
    for (const name of JIRA_ENV) {
      if (saved[name] === undefined) delete process.env[name];
      else process.env[name] = saved[name];
    }
  });
  for (const name of JIRA_ENV) process.env[name] = values[name];
}

// The audit must identify each of the four drift
// classes independently and report ok:true only when none are present.

test('reports ok when Jira options exactly match the catalog', () => {
  const result = diffAgentFieldOptions(
    ['backend-agent', 'frontend-agent'],
    [],
    [
      { value: 'backend-agent', disabled: false },
      { value: 'frontend-agent', disabled: false },
    ]
  );
  assert.deepEqual(result, { missing: [], unexpected: [], mismatched: [], retiredButEnabled: [], ok: true });
});

test('reports a catalog id with no Jira option as missing', () => {
  const result = diffAgentFieldOptions(['backend-agent', 'devops-agent'], [], [
    { value: 'backend-agent', disabled: false },
  ]);
  assert.deepEqual(result.missing, ['devops-agent']);
  assert.equal(result.ok, false);
});

test('reports a Jira option with no catalog or retired backing as unexpected', () => {
  const result = diffAgentFieldOptions(['backend-agent'], [], [
    { value: 'backend-agent', disabled: false },
    { value: 'mystery-agent', disabled: false },
  ]);
  assert.deepEqual(result.unexpected, ['mystery-agent']);
  assert.equal(result.ok, false);
});

test('reports an active catalog id whose Jira option is disabled as mismatched', () => {
  const result = diffAgentFieldOptions(['backend-agent'], [], [
    { value: 'backend-agent', disabled: true },
  ]);
  assert.deepEqual(result.mismatched, ['backend-agent']);
  assert.equal(result.ok, false);
});

test('reports a retired id whose Jira option is still enabled as retired-but-enabled', () => {
  const result = diffAgentFieldOptions(['backend-agent'], ['qa-agent'], [
    { value: 'backend-agent', disabled: false },
    { value: 'qa-agent', disabled: false },
  ]);
  assert.deepEqual(result.retiredButEnabled, ['qa-agent']);
  assert.equal(result.ok, false);
});

test('a properly disabled retired option is not reported as drift', () => {
  const result = diffAgentFieldOptions(['backend-agent'], ['qa-agent'], [
    { value: 'backend-agent', disabled: false },
    { value: 'qa-agent', disabled: true },
  ]);
  assert.deepEqual(result, { missing: [], unexpected: [], mismatched: [], retiredButEnabled: [], ok: true });
});

test('all four drift classes can be reported together', () => {
  const result = diffAgentFieldOptions(
    ['backend-agent', 'devops-agent'],
    ['qa-agent'],
    [
      { value: 'backend-agent', disabled: true },   // mismatched
      { value: 'qa-agent', disabled: false },        // retired-but-enabled
      { value: 'mystery-agent', disabled: false },   // unexpected
      // devops-agent absent entirely                // missing
    ]
  );
  assert.deepEqual(result.missing, ['devops-agent']);
  assert.deepEqual(result.unexpected, ['mystery-agent']);
  assert.deepEqual(result.mismatched, ['backend-agent']);
  assert.deepEqual(result.retiredButEnabled, ['qa-agent']);
  assert.equal(result.ok, false);
});

// The drift audit is the one thing ScrumMaster calls Jira for on a timer
// rather than in response to a Jira-backed work item, so it is the one
// thing an installation with no Jira connection still calls. Left
// unguarded, it addressed the shipped template's placeholder host at every
// boot and logged the failure.

test('the audit does not call Jira when the environment still holds the shipped placeholders', async (t) => {
  setJiraEnv(t, TEMPLATE_PLACEHOLDERS);
  let called = false;
  t.mock.method(jira, 'getAgentFieldOptions', async () => { called = true; return []; });

  const result = await auditAgentFieldDrift();

  assert.equal(result, null, 'an audit with nothing to audit reports no result, not "no drift"');
  assert.equal(called, false, 'no Jira request may be made at all');
});

test('scheduling the audit with no Jira configured starts no timer and makes no request', (t) => {
  setJiraEnv(t, TEMPLATE_PLACEHOLDERS);
  let called = false;
  t.mock.method(jira, 'getAgentFieldOptions', async () => { called = true; return []; });

  const timer = scheduleAgentFieldAudit(50);

  assert.equal(timer, null, 'no interval timer may be left behind');
  assert.equal(called, false);
});

test('a configured Jira is still audited at startup', async (t) => {
  setJiraEnv(t, CONFIGURED);
  let called = false;
  t.mock.method(jira, 'getAgentFieldOptions', async () => {
    called = true;
    return [{ value: 'refinement-agent', disabled: false }, { value: 'backend-agent', disabled: false }];
  });

  const result = await auditAgentFieldDrift();

  assert.equal(called, true, 'the skip must be scoped to an unconfigured installation');
  assert.ok(result, 'a configured installation still gets a drift result');
  assert.deepEqual(result.missing, ['frontend-agent'], 'the catalog fixture has an agent Jira does not');
});
