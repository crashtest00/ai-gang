'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { fetchJenkinsJobState, extractJobState } = require('../scripts/lib/jenkins-api');

// ---- extractJobState (pure JSON-shape parsing) --------------------------

test('extractJobState: multibranch child building -> job/branch key, parent recorded as multibranch', () => {
  const body = {
    jobs: [
      {
        name: 'hello-world-pipeline',
        jobs: [
          { name: 'dev', lastBuild: { building: false } },
          { name: 'PR-3', lastBuild: { building: true } },
        ],
      },
    ],
  };
  const { buildingKeys, multibranchJobNames } = extractJobState(body);
  assert.deepEqual(buildingKeys, new Set(['hello-world-pipeline/PR-3']));
  assert.deepEqual(multibranchJobNames, new Set(['hello-world-pipeline']));
});

test('extractJobState: singleton job building -> bare job name key, not recorded as multibranch', () => {
  const body = {
    jobs: [
      { name: 'release-candidate', lastBuild: { building: true } },
      { name: 'production-promote', lastBuild: { building: false } },
    ],
  };
  const { buildingKeys, multibranchJobNames } = extractJobState(body);
  assert.deepEqual(buildingKeys, new Set(['release-candidate']));
  assert.deepEqual(multibranchJobNames, new Set());
});

test('extractJobState: a multibranch parent with no jobs array entries yet is still recorded as multibranch', () => {
  const body = { jobs: [{ name: 'brand-new-multibranch', jobs: [] }] };
  const { buildingKeys, multibranchJobNames } = extractJobState(body);
  assert.deepEqual(buildingKeys, new Set());
  assert.deepEqual(multibranchJobNames, new Set(['brand-new-multibranch']), 'presence of the jobs array, not its length, is the signal');
});

test('extractJobState: no jobs building -> empty building set', () => {
  const body = { jobs: [{ name: 'release-candidate', lastBuild: { building: false } }] };
  assert.deepEqual(extractJobState(body).buildingKeys, new Set());
});

test('extractJobState: job that has never built (no lastBuild) is not building', () => {
  const body = { jobs: [{ name: 'brand-new-job' }] };
  assert.deepEqual(extractJobState(body).buildingKeys, new Set());
});

test('extractJobState: empty jobs list -> empty sets', () => {
  const { buildingKeys, multibranchJobNames } = extractJobState({ jobs: [] });
  assert.deepEqual(buildingKeys, new Set());
  assert.deepEqual(multibranchJobNames, new Set());
});

test('extractJobState: a mix of multibranch and singleton jobs, matching jenkins/jenkins.yaml\'s real job set', () => {
  const body = {
    jobs: [
      { name: 'release-candidate', lastBuild: { building: false } },
      { name: 'production-promote', lastBuild: { building: true } },
      { name: 'release-preview-teardown', lastBuild: null },
      {
        name: 'hello-world-pipeline',
        jobs: [
          { name: 'dev', lastBuild: { building: true } },
          { name: 'PR-7', lastBuild: { building: false } },
        ],
      },
    ],
  };
  const { buildingKeys, multibranchJobNames } = extractJobState(body);
  assert.deepEqual(buildingKeys, new Set(['production-promote', 'hello-world-pipeline/dev']));
  assert.deepEqual(multibranchJobNames, new Set(['hello-world-pipeline']));
});

// ---- fetchJenkinsJobState (injected fetchFn) -----------------------------

test('fetchJenkinsJobState: calls the Jenkins API URL and parses the result', async () => {
  let calledUrl;
  let calledHeaders;
  const fakeFetch = async (url, init) => {
    calledUrl = url;
    calledHeaders = init.headers;
    return {
      ok: true,
      status: 200,
      statusText: 'OK',
      json: async () => ({ jobs: [{ name: 'release-candidate', lastBuild: { building: true } }] }),
    };
  };

  const result = await fetchJenkinsJobState({
    jenkinsUrl: 'http://localhost:8080',
    user: 'admin',
    token: 'secret',
    fetchFn: fakeFetch,
  });

  assert.deepEqual(result.buildingKeys, new Set(['release-candidate']));
  assert.match(calledUrl, /^http:\/\/localhost:8080\/api\/json\?tree=/);
  assert.equal(calledHeaders.Authorization, `Basic ${Buffer.from('admin:secret').toString('base64')}`);
});

test('fetchJenkinsJobState: throws on a non-OK response rather than silently returning empty', async () => {
  const fakeFetch = async () => ({ ok: false, status: 503, statusText: 'Service Unavailable' });
  await assert.rejects(
    () => fetchJenkinsJobState({ jenkinsUrl: 'http://localhost:8080', fetchFn: fakeFetch }),
    /503/,
  );
});

test('fetchJenkinsJobState: strips a trailing slash from jenkinsUrl', async () => {
  let calledUrl;
  const fakeFetch = async (url) => {
    calledUrl = url;
    return { ok: true, json: async () => ({ jobs: [] }) };
  };
  await fetchJenkinsJobState({ jenkinsUrl: 'http://localhost:8080/', fetchFn: fakeFetch });
  assert.equal(calledUrl.startsWith('http://localhost:8080/api/json'), true);
  assert.equal(calledUrl.includes('8080//api'), false);
});
