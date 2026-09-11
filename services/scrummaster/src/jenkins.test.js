'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const axios = require('axios');

const jenkins = require('./jenkins');

// jenkins.js has no shared client instance (unlike jira.js's axios.create()),
// so tests mock axios.post directly rather than a module-local export.
function withJenkinsUrl(url, fn) {
  const prev = process.env.JENKINS_URL;
  if (url === undefined) delete process.env.JENKINS_URL;
  else process.env.JENKINS_URL = url;

  return fn().finally(() => {
    if (prev === undefined) delete process.env.JENKINS_URL;
    else process.env.JENKINS_URL = prev;
  });
}

test('triggerReleaseCandidate posts to the release-candidate webhook with the issue and project', async (t) => {
  let call = null;
  t.mock.method(axios, 'post', async (url, payload, config) => { call = { url, payload, config }; });

  await withJenkinsUrl('https://jenkins.example.com', () =>
    jenkins.triggerReleaseCandidate('GANG-42', 'hello-world')
  );

  assert.equal(call.url, 'https://jenkins.example.com/generic-webhook-trigger/invoke');
  assert.deepEqual(call.payload, { issueKey: 'GANG-42', projectName: 'hello-world' });
  assert.deepEqual(call.config, { params: { token: 'release-candidate' } });
});

test('triggerReleaseCandidate (local mode) posts workItemId instead of issueKey', async (t) => {
  let call = null;
  t.mock.method(axios, 'post', async (url, payload, config) => { call = { url, payload, config }; });

  await withJenkinsUrl('https://jenkins.example.com', () =>
    jenkins.triggerReleaseCandidate({ workItemId: 'wi-release-1' }, 'hello-world')
  );

  assert.deepEqual(call.payload, { workItemId: 'wi-release-1', projectName: 'hello-world' });
  assert.equal('issueKey' in call.payload, false);
});

test('triggerProductionPromote posts to the production-promote webhook with the candidate SHA', async (t) => {
  let call = null;
  t.mock.method(axios, 'post', async (url, payload, config) => { call = { url, payload, config }; });

  await withJenkinsUrl('https://jenkins.example.com', () =>
    jenkins.triggerProductionPromote('GANG-42', 'hello-world', 'abc1234')
  );

  assert.equal(call.url, 'https://jenkins.example.com/generic-webhook-trigger/invoke');
  assert.deepEqual(call.payload, { issueKey: 'GANG-42', projectName: 'hello-world', candidateSha: 'abc1234' });
  assert.deepEqual(call.config, { params: { token: 'production-promote' } });
});

test('triggerProductionPromote (local mode) posts workItemId instead of issueKey', async (t) => {
  let call = null;
  t.mock.method(axios, 'post', async (url, payload, config) => { call = { url, payload, config }; });

  await withJenkinsUrl('https://jenkins.example.com', () =>
    jenkins.triggerProductionPromote({ workItemId: 'wi-release-1' }, 'hello-world', 'abc1234')
  );

  assert.deepEqual(call.payload, { workItemId: 'wi-release-1', projectName: 'hello-world', candidateSha: 'abc1234' });
});

test('triggerPreviewTeardown posts to the release-preview-teardown webhook', async (t) => {
  let call = null;
  t.mock.method(axios, 'post', async (url, payload, config) => { call = { url, payload, config }; });

  await withJenkinsUrl('https://jenkins.example.com', () =>
    jenkins.triggerPreviewTeardown('GANG-42', 'hello-world')
  );

  assert.equal(call.url, 'https://jenkins.example.com/generic-webhook-trigger/invoke');
  assert.deepEqual(call.payload, { issueKey: 'GANG-42', projectName: 'hello-world' });
  assert.deepEqual(call.config, { params: { token: 'release-preview-teardown' } });
});

test('triggerPreviewTeardown (local mode) posts workItemId instead of issueKey', async (t) => {
  let call = null;
  t.mock.method(axios, 'post', async (url, payload, config) => { call = { url, payload, config }; });

  await withJenkinsUrl('https://jenkins.example.com', () =>
    jenkins.triggerPreviewTeardown({ workItemId: 'wi-release-1' }, 'hello-world')
  );

  assert.deepEqual(call.payload, { workItemId: 'wi-release-1', projectName: 'hello-world' });
});

test('a trailing slash on JENKINS_URL is stripped before appending the webhook path', async (t) => {
  let seenUrl = null;
  t.mock.method(axios, 'post', async (url) => { seenUrl = url; });

  await withJenkinsUrl('https://jenkins.example.com/', () =>
    jenkins.triggerReleaseCandidate('GANG-42', 'hello-world')
  );

  assert.equal(seenUrl, 'https://jenkins.example.com/generic-webhook-trigger/invoke');
});

test('each trigger function no-ops when JENKINS_URL is unset', async (t) => {
  let called = false;
  t.mock.method(axios, 'post', async () => { called = true; });

  await withJenkinsUrl(undefined, async () => {
    await jenkins.triggerReleaseCandidate('GANG-42', 'hello-world');
    await jenkins.triggerProductionPromote('GANG-42', 'hello-world', 'abc1234');
    await jenkins.triggerPreviewTeardown('GANG-42', 'hello-world');
  });

  assert.equal(called, false);
});

test('an axios rejection propagates to the caller rather than being swallowed', async (t) => {
  t.mock.method(axios, 'post', async () => { throw new Error('ECONNREFUSED'); });

  await assert.rejects(
    () => withJenkinsUrl('https://jenkins.example.com', () => jenkins.triggerReleaseCandidate('GANG-42', 'hello-world')),
    /ECONNREFUSED/
  );
});
