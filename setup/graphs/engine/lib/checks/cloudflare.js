'use strict';

// Detection logic backing the Cloudflare pilot graph's four decision nodes
// (graph-process-engine.md REQ-10). Each function takes an injectable
// `httpClient` (defaulting to Node's global fetch) so tests can mock every
// external Cloudflare API call per this workstream's test-coverage
// requirement ("Mock external Cloudflare API calls in automated tests — do
// not hit a real Cloudflare account from a test suite").
//
// Each function returns exactly one of its owning decision node's declared
// `when` outcome keys, matching setup/graphs/cloudflare-setup.graph.yaml.

const CF_API = 'https://api.cloudflare.com/client/v4';

function defaultHttpClient() {
  return globalThis.fetch;
}

/** cf-token-present: "present" | "absent" | "probe-error" */
async function checkTokenPresent(env, httpClient = defaultHttpClient()) {
  const token = env.CF_API_KEY;
  if (!token) return 'absent';
  try {
    const res = await httpClient(`${CF_API}/user/tokens/verify`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    const body = await res.json();
    if (body && body.success === true) return 'present';
    if (res.status === 401 || res.status === 403) return 'absent';
    return 'probe-error';
  } catch {
    return 'probe-error';
  }
}

/** cf-tunnel-exists: "exists" | "absent" | "probe-error" */
async function checkTunnelExists(env, httpClient = defaultHttpClient(), tunnelName = 'ai-gang') {
  if (!env.CF_API_KEY || !env.CF_ACCOUNT_ID) return 'probe-error';
  try {
    const res = await httpClient(
      `${CF_API}/accounts/${env.CF_ACCOUNT_ID}/cfd_tunnel?name=${encodeURIComponent(tunnelName)}&is_deleted=false`,
      { headers: { Authorization: `Bearer ${env.CF_API_KEY}` } }
    );
    const body = await res.json();
    if (!body || body.success !== true) return 'probe-error';
    return Array.isArray(body.result) && body.result.length > 0 ? 'exists' : 'absent';
  } catch {
    return 'probe-error';
  }
}

/**
 * cf-subdomain-vars-check:
 *   "none-requested"          — neither PREVIEW_SUBDOMAIN nor BETA_DOMAIN set
 *   "complete"                — PREVIEW_SUBDOMAIN and/or BETA_DOMAIN set,
 *                                and BETA_VM_HOST also set
 *   "missing-beta-vm-host"    — PREVIEW_SUBDOMAIN or BETA_DOMAIN set, but
 *                                BETA_VM_HOST is not
 *   "probe-error"             — unreachable for a pure env-var read; present
 *                                only for decision-node contract uniformity
 *                                (REQ-02) and for a check function that
 *                                throws unexpectedly
 */
function checkSubdomainVars(env) {
  try {
    const wantsAccess = Boolean(env.PREVIEW_SUBDOMAIN) || Boolean(env.BETA_DOMAIN);
    if (!wantsAccess) return 'none-requested';
    return env.BETA_VM_HOST ? 'complete' : 'missing-beta-vm-host';
  } catch {
    return 'probe-error';
  }
}

/** cf-account-id-check: "present" | "absent" */
function checkCfAccountId(env) {
  return env.CF_ACCOUNT_ID ? 'present' : 'absent';
}

module.exports = { checkTokenPresent, checkTunnelExists, checkSubdomainVars, checkCfAccountId };
