'use strict';

// Thin, injectable client for the two Jenkins REST API questions the
// workspace prune job needs answered, both from one call:
//   1. Which job/branch combinations have a build running right now?
//      (REQ-04's exclusion list for workspace pruning.)
//   2. Which top-level jobs are multibranch parents (so their on-disk
//      workspace layout is <job>/<branch>/) versus singleton pipeline jobs
//      (workspace layout is just <job>/)?
// (2) matters because a singleton job's checkout can perfectly well contain
// subdirectories of its own (src/, .git/, node_modules/...) — filesystem
// shape alone can't tell a multibranch parent apart from an ordinary
// checkout that happens to have subdirectories, so this asks Jenkins
// directly instead of guessing from what's on disk. Jenkins' own API
// answers it for free: a multibranch project's JSON entry has a `jobs`
// array (one entry per branch/PR); a singleton pipeline job's doesn't.
//
// Kept separate from retention-policy.js (pure) and prune-workspaces.js
// (orchestration) so it can be unit tested with a fake `fetchFn` instead of
// a real Jenkins controller, and so prune-workspaces.js's own tests can
// inject canned job-state without going through HTTP at all.

/**
 * @param {object} opts
 * @param {string} opts.jenkinsUrl - e.g. http://localhost:8080
 * @param {string} [opts.user]
 * @param {string} [opts.token] - admin password or API token
 * @param {typeof fetch} [opts.fetchFn] - injectable for tests; defaults to global fetch
 * @returns {Promise<{buildingKeys: Set<string>, multibranchJobNames: Set<string>}>}
 *   buildingKeys: "job/branch" (or bare "job" for a singleton job) with a
 *   currently building lastBuild. multibranchJobNames: top-level job names
 *   that are multibranch parents.
 */
async function fetchJenkinsJobState(opts) {
  const { jenkinsUrl, user, token, fetchFn = globalThis.fetch } = opts;
  if (!fetchFn) {
    throw new Error('fetchJenkinsJobState: no fetch implementation available (Node < 18?) and none injected');
  }

  const url = `${jenkinsUrl.replace(/\/$/, '')}/api/json?tree=jobs[name,lastBuild[building],jobs[name,lastBuild[building]]]`;
  const headers = {};
  if (user && token) {
    headers.Authorization = `Basic ${Buffer.from(`${user}:${token}`).toString('base64')}`;
  }

  const res = await fetchFn(url, { headers });
  if (!res.ok) {
    throw new Error(`fetchJenkinsJobState: Jenkins API returned ${res.status} ${res.statusText}`);
  }
  const body = await res.json();
  return extractJobState(body);
}

/**
 * Pure part of the API response handling, split out so the JSON-shape
 * parsing itself is unit-testable without any network/fetch mocking at
 * all — only fetchJenkinsJobState above needs an injected fetchFn.
 *
 * @param {object} apiResponse - parsed body of GET /api/json?tree=jobs[...]
 * @returns {{buildingKeys: Set<string>, multibranchJobNames: Set<string>}}
 */
function extractJobState(apiResponse) {
  const buildingKeys = new Set();
  const multibranchJobNames = new Set();

  for (const job of apiResponse.jobs || []) {
    if (Array.isArray(job.jobs)) {
      // Multibranch parent (has a `jobs` array at all, even if currently
      // empty — that's still the authoritative signal, not the count).
      multibranchJobNames.add(job.name);
      for (const branchJob of job.jobs) {
        if (branchJob.lastBuild && branchJob.lastBuild.building) {
          buildingKeys.add(`${job.name}/${branchJob.name}`);
        }
      }
    } else if (job.lastBuild && job.lastBuild.building) {
      // Singleton pipeline job — the job name IS the workspace directory.
      buildingKeys.add(job.name);
    }
  }
  return { buildingKeys, multibranchJobNames };
}

module.exports = { fetchJenkinsJobState, extractJobState };
