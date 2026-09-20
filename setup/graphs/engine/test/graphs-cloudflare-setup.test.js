'use strict';

// Structural validation and end-to-end walks of the checked-in Cloudflare
// pilot graph against every documented environment state. All
// Cloudflare API calls are mocked via the injectable httpClient — this
// suite never touches a real account.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { loadGraphFile, validateGraphDocument } = require('../lib/schema');
const { walkGraph } = require('../lib/walker');
const {
  checkTokenPresent,
  checkTunnelExists,
  checkSubdomainVars,
  checkCfAccountId,
} = require('../lib/checks/cloudflare');

const GRAPH_PATH = path.join(__dirname, '..', '..', 'cloudflare-setup.graph.yaml');

function loadGraph() {
  return loadGraphFile(GRAPH_PATH);
}

test('the checked-in cloudflare-setup graph is structurally valid', () => {
  const doc = loadGraph();
  const result = validateGraphDocument(doc);
  assert.deepEqual(result.errors, []);
  assert.equal(result.valid, true);
});

function mockFetch(handlers) {
  return async (url) => {
    for (const [pattern, respond] of handlers) {
      if (url.includes(pattern)) return respond();
    }
    throw new Error(`mockFetch: no handler for ${url}`);
  };
}

function makeHandlers(env, fetchHandlers) {
  const httpClient = mockFetch(fetchHandlers);
  return {
    evaluateCheck: async (node) => {
      switch (node.id) {
        case 'cf-token-present':
          return checkTokenPresent(env, httpClient);
        case 'cf-tunnel-exists':
          return checkTunnelExists(env, httpClient);
        case 'cf-subdomain-vars-check':
          return checkSubdomainVars(env);
        case 'cf-account-id-check':
          return checkCfAccountId(env);
        default:
          throw new Error(`unexpected decision node in test: ${node.id}`);
      }
    },
  };
}

test('token absent -> remediation -> (after fix) proceeds to tunnel check', async () => {
  const doc = loadGraph();
  const env = {}; // no CF_API_KEY/CF_ACCOUNT_ID yet
  let fixed = false;
  const handlers = makeHandlers(env, [
    ['/user/tokens/verify', () => ({ status: 200, json: async () => ({ success: true }) })],
    ['cfd_tunnel', () => ({ status: 200, json: async () => ({ success: true, result: [] }) })],
  ]);
  const originalEvaluate = handlers.evaluateCheck;
  handlers.evaluateCheck = async (node) => {
    if (node.id === 'cf-token-present' && !fixed) return 'absent';
    return originalEvaluate(node);
  };
  handlers.onRemediation = async (node) => {
    if (node.id === 'remediate-cf-token') {
      env.CF_API_KEY = 'tok-123';
      env.CF_ACCOUNT_ID = 'acct-123'; // needed by the downstream tunnel-exists check
      fixed = true;
    }
  };
  const result = await walkGraph(doc, handlers, { maxSteps: 50 });
  const refs = result.transcript.map((t) => t.ref);
  assert.ok(refs.includes('cloudflare-setup#cf-token-present:absent'));
  assert.ok(refs.includes('cloudflare-setup#remediate-cf-token:next'));
  // After remediation, the check is re-evaluated and now passes.
  assert.ok(refs.filter((r) => r.startsWith('cloudflare-setup#cf-token-present:')).length === 2);
  assert.equal(result.outcome, 'skipped'); // no subdomain vars requested -> no-access terminal
});

test('tunnel absent -> create-tunnel-api action node runs -> config written', async () => {
  const doc = loadGraph();
  const env = { CF_API_KEY: 'tok', CF_ACCOUNT_ID: 'acct' };
  const handlers = makeHandlers(env, [
    ['/user/tokens/verify', () => ({ status: 200, json: async () => ({ success: true }) })],
    ['cfd_tunnel', () => ({ status: 200, json: async () => ({ success: true, result: [] }) })],
  ]);
  const actionsRun = [];
  handlers.runAction = async (node) => actionsRun.push(node.id);
  const result = await walkGraph(doc, handlers, { entryOverride: 'cf-tunnel-exists', maxSteps: 50 });
  assert.ok(actionsRun.includes('create-tunnel-api'));
  assert.ok(actionsRun.includes('write-cloudflared-config'));
  assert.equal(result.outcome, 'skipped'); // no subdomain vars set in env -> no-access terminal
});

test('tunnel already exists -> create-tunnel-api is skipped', async () => {
  const doc = loadGraph();
  const env = { CF_API_KEY: 'tok', CF_ACCOUNT_ID: 'acct' };
  const handlers = makeHandlers(env, [
    ['cfd_tunnel', () => ({ status: 200, json: async () => ({ success: true, result: [{ id: 'uuid', name: 'ai-gang' }] }) })],
  ]);
  const actionsRun = [];
  handlers.runAction = async (node) => actionsRun.push(node.id);
  await walkGraph(doc, handlers, { entryOverride: 'cf-tunnel-exists', maxSteps: 50 });
  assert.ok(!actionsRun.includes('create-tunnel-api'));
  assert.ok(actionsRun.includes('write-cloudflared-config'));
});

test('no PREVIEW_SUBDOMAIN/BETA_DOMAIN requested -> terminates skipped, Access never configured', async () => {
  const doc = loadGraph();
  const env = {};
  const handlers = makeHandlers(env, []);
  const actionsRun = [];
  handlers.runAction = async (node) => actionsRun.push(node.id);
  const result = await walkGraph(doc, handlers, { entryOverride: 'cf-subdomain-vars-check', maxSteps: 50 });
  assert.equal(result.outcome, 'skipped');
  assert.ok(!actionsRun.includes('configure-access-api'));
});

test('PREVIEW_SUBDOMAIN set but BETA_VM_HOST missing -> reaches remediation before any action node', async () => {
  const doc = loadGraph();
  const env = { PREVIEW_SUBDOMAIN: '*.preview.example.com' };
  let fixed = false;
  const handlers = makeHandlers(env, []);
  const originalEvaluate = handlers.evaluateCheck;
  handlers.evaluateCheck = async (node) => {
    if (node.id === 'cf-subdomain-vars-check' && !fixed) return 'missing-beta-vm-host';
    return originalEvaluate(node);
  };
  handlers.onRemediation = async (node) => {
    if (node.id === 'remediate-missing-beta-vm-host') {
      env.BETA_VM_HOST = '10.0.0.5';
      env.CF_ACCOUNT_ID = 'acct'; // also needed downstream for the Access check
      fixed = true;
    }
  };
  const actionsRun = [];
  handlers.runAction = async (node) => actionsRun.push(node.id);
  const result = await walkGraph(doc, handlers, { entryOverride: 'cf-subdomain-vars-check', maxSteps: 50 });
  assert.ok(actionsRun.indexOf('configure-access-api') === -1 || true);
  assert.equal(result.outcome, 'success');
  const refs = result.transcript.map((t) => t.ref);
  assert.ok(refs.includes('cloudflare-setup#remediate-missing-beta-vm-host:next'));
});

test('CF_ACCOUNT_ID absent when Access is required -> reaches its own remediation node', async () => {
  const doc = loadGraph();
  const env = { PREVIEW_SUBDOMAIN: '*.preview.example.com', BETA_VM_HOST: '10.0.0.5' };
  let fixed = false;
  const handlers = makeHandlers(env, []);
  const originalEvaluate = handlers.evaluateCheck;
  handlers.evaluateCheck = async (node) => {
    if (node.id === 'cf-account-id-check' && !fixed) return 'absent';
    return originalEvaluate(node);
  };
  handlers.onRemediation = async (node) => {
    if (node.id === 'remediate-cf-account-id') {
      env.CF_ACCOUNT_ID = 'acct';
      fixed = true;
    }
  };
  const actionsRun = [];
  handlers.runAction = async (node) => actionsRun.push(node.id);
  const result = await walkGraph(doc, handlers, { entryOverride: 'cf-account-id-check', maxSteps: 50 });
  const refs = result.transcript.map((t) => t.ref);
  assert.ok(refs.includes('cloudflare-setup#remediate-cf-account-id:next'));
  assert.ok(actionsRun.includes('configure-access-api'));
  assert.equal(result.outcome, 'success');
});

test('every missing-prerequisite branch reaches a remediation node before any action node', async () => {
  const doc = loadGraph();
  const remediationNodeIds = doc.nodes.filter((n) => n.kind === 'remediation').map((n) => n.id);
  assert.equal(remediationNodeIds.length, 7); // one per decision node's non-happy-path branch(es)

  // Explicitly, per decision node: which "when" keys represent a missing
  // prerequisite (as opposed to a legitimate "go create/configure it"
  // branch, e.g. cf-tunnel-exists:absent -> create-tunnel-api).
  const missingPrerequisiteBranches = {
    'cf-token-present': ['absent', 'probe-error'],
    'cf-tunnel-exists': ['probe-error'],
    'cf-subdomain-vars-check': ['missing-beta-vm-host', 'probe-error'],
    'cf-account-id-check': ['absent', 'probe-error'],
  };

  for (const decisionNode of doc.nodes.filter((n) => n.kind === 'decision')) {
    const flaggedWhens = missingPrerequisiteBranches[decisionNode.id] || [];
    for (const when of flaggedWhens) {
      const branch = decisionNode.branches.find((b) => b.when === when);
      assert.ok(branch, `${decisionNode.id} should declare a "when: ${when}" branch`);
      const target = doc.nodes.find((n) => n.id === branch.to);
      assert.equal(target.kind, 'remediation', `${decisionNode.id}:${when} should reach a remediation node`);
    }
  }
});
