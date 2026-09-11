'use strict';

// Cloudflare pilot graph branch-point detection logic. Every Cloudflare API
// call is mocked here — this suite never touches a real Cloudflare account
// (per this workstream's non-negotiable test-coverage requirement).

const { test } = require('node:test');
const assert = require('node:assert/strict');
const {
  checkTokenPresent,
  checkTunnelExists,
  checkSubdomainVars,
  checkCfAccountId,
} = require('../lib/checks/cloudflare');

function mockFetch(response) {
  return async () => ({
    status: response.status ?? 200,
    json: async () => response.body,
  });
}

function mockFetchThrows() {
  return async () => {
    throw new Error('network unreachable');
  };
}

// --- cf-token-present ---

test('checkTokenPresent: "absent" when CF_API_KEY is unset', async () => {
  const outcome = await checkTokenPresent({}, mockFetch({ body: { success: true } }));
  assert.equal(outcome, 'absent');
});

test('checkTokenPresent: "present" when the token verifies successfully', async () => {
  const outcome = await checkTokenPresent({ CF_API_KEY: 'tok' }, mockFetch({ status: 200, body: { success: true } }));
  assert.equal(outcome, 'present');
});

test('checkTokenPresent: "absent" when the API reports the token invalid (401)', async () => {
  const outcome = await checkTokenPresent(
    { CF_API_KEY: 'bad-tok' },
    mockFetch({ status: 401, body: { success: false, errors: [{ message: 'invalid token' }] } })
  );
  assert.equal(outcome, 'absent');
});

test('checkTokenPresent: "probe-error" when the API call itself fails to execute', async () => {
  const outcome = await checkTokenPresent({ CF_API_KEY: 'tok' }, mockFetchThrows());
  assert.equal(outcome, 'probe-error');
});

// --- cf-tunnel-exists ---

test('checkTunnelExists: "probe-error" when required env is missing (can\'t even form the request)', async () => {
  const outcome = await checkTunnelExists({}, mockFetch({ body: {} }));
  assert.equal(outcome, 'probe-error');
});

test('checkTunnelExists: "exists" when the named tunnel is returned', async () => {
  const outcome = await checkTunnelExists(
    { CF_API_KEY: 'tok', CF_ACCOUNT_ID: 'acct' },
    mockFetch({ body: { success: true, result: [{ id: 'uuid-1', name: 'ai-gang' }] } })
  );
  assert.equal(outcome, 'exists');
});

test('checkTunnelExists: "absent" when no matching tunnel is returned', async () => {
  const outcome = await checkTunnelExists(
    { CF_API_KEY: 'tok', CF_ACCOUNT_ID: 'acct' },
    mockFetch({ body: { success: true, result: [] } })
  );
  assert.equal(outcome, 'absent');
});

test('checkTunnelExists: "probe-error" on API failure response', async () => {
  const outcome = await checkTunnelExists(
    { CF_API_KEY: 'tok', CF_ACCOUNT_ID: 'acct' },
    mockFetch({ body: { success: false, errors: [{ message: 'boom' }] } })
  );
  assert.equal(outcome, 'probe-error');
});

test('checkTunnelExists: "probe-error" when the network call throws', async () => {
  const outcome = await checkTunnelExists({ CF_API_KEY: 'tok', CF_ACCOUNT_ID: 'acct' }, mockFetchThrows());
  assert.equal(outcome, 'probe-error');
});

// --- cf-subdomain-vars-check ---

test('checkSubdomainVars: "none-requested" when neither PREVIEW_SUBDOMAIN nor BETA_DOMAIN is set', () => {
  assert.equal(checkSubdomainVars({}), 'none-requested');
});

test('checkSubdomainVars: "complete" when PREVIEW_SUBDOMAIN and BETA_VM_HOST are both set', () => {
  assert.equal(
    checkSubdomainVars({ PREVIEW_SUBDOMAIN: '*.preview.example.com', BETA_VM_HOST: '10.0.0.5' }),
    'complete'
  );
});

test('checkSubdomainVars: "complete" when BETA_DOMAIN and BETA_VM_HOST are both set', () => {
  assert.equal(checkSubdomainVars({ BETA_DOMAIN: '*.beta.example.com', BETA_VM_HOST: '10.0.0.5' }), 'complete');
});

test('checkSubdomainVars: "missing-beta-vm-host" when PREVIEW_SUBDOMAIN is set but BETA_VM_HOST is not', () => {
  assert.equal(checkSubdomainVars({ PREVIEW_SUBDOMAIN: '*.preview.example.com' }), 'missing-beta-vm-host');
});

test('checkSubdomainVars: "missing-beta-vm-host" when BETA_DOMAIN is set but BETA_VM_HOST is not', () => {
  assert.equal(checkSubdomainVars({ BETA_DOMAIN: '*.beta.example.com' }), 'missing-beta-vm-host');
});

// --- cf-account-id-check ---

test('checkCfAccountId: "present" / "absent"', () => {
  assert.equal(checkCfAccountId({ CF_ACCOUNT_ID: 'acct-1' }), 'present');
  assert.equal(checkCfAccountId({}), 'absent');
});
