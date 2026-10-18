'use strict';

// REQ-12 (V5.3 release-mode parity): the release-candidate job's report step
// must send the native build's URL and status under the exact names the core
// endpoint reads. This test reads the real jenkins.yaml text and the real
// services/core/workitems/views.py; it does not retype the request body.
//
// What is exercised: the stage's literal Groovy lines for the condition, the
// `nativeFields` string building and the curl `-d` payload template are
// extracted from jenkins.yaml. The condition and the string concatenation are
// valid JavaScript as written, so they are evaluated as-is against a fake
// `env`; the `^${NAME}` placeholders (JCasC escape for Groovy `${NAME}`) are
// then substituted the way Groovy's GString would. The result is parsed as JSON.
// What is not exercised: a real Groovy / JCasC / Job DSL parse, the shell's
// handling of the single-quoted -d argument, or curl itself.

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const YAML = fs.readFileSync(path.join(__dirname, '..', 'jenkins.yaml'), 'utf8');
const VIEWS = fs.readFileSync(
  path.join(__dirname, '..', '..', 'services', 'core', 'workitems', 'views.py'), 'utf8');

function reportStage() {
  const start = YAML.indexOf("stage('Report candidate back to the release')");
  assert.ok(start >= 0, "report stage not found in jenkins.yaml");
  const end = YAML.indexOf('post {', start);
  assert.ok(end > start, 'end of report stage not found');
  return YAML.slice(start, end);
}

function extractLogic() {
  const stage = reportStage();
  const cond = stage.match(/^\s*if \((.+)\) \{\s*$/m);
  assert.ok(cond, 'native-build condition not found in the report stage');
  const assign = stage.match(/^\s*nativeFields = (.+)$/m);
  assert.ok(assign, 'nativeFields assignment not found in the report stage');
  const payload = stage.match(/-d '(\{.*\})'\s*$/m);
  assert.ok(payload, 'curl -d payload not found in the report stage');
  return { cond: cond[1], assign: assign[1], payload: payload[1] };
}

// Run the stage's own lines for one set of env values; return the parsed body.
function buildBody(env) {
  const { cond, assign, payload } = extractLogic();
  const fn = new Function('env', `
    let nativeFields = '';
    if (${cond}) {
      nativeFields = ${assign};
    }
    return nativeFields;
  `);
  const nativeFields = fn(env);
  const vars = {
    WORK_ITEM_ID: 'wi-1', CANDIDATE_SHA: 'abc123', BUILD_IDENTIFIER: 'build-7',
    PREVIEW_URL: 'https://preview.example/abc123', nativeFields,
  };
  const raw = payload.replace(/\^\$\{(\w+)\}/g, (m, name) => {
    assert.ok(name in vars, `unexpected placeholder ${m} in the payload`);
    return vars[name];
  });
  return JSON.parse(raw); // throws if the body is not valid JSON
}

function assertBase(body) {
  assert.equal(body.candidateSha, 'abc123');
  assert.equal(body.buildIdentifier, 'build-7');
  assert.equal(body.previewUrl, 'https://preview.example/abc123');
}

test('report body: native build ran -> valid JSON with base fields plus nativeBuildUrl/nativeBuildStatus', () => {
  const body = buildBody({ NATIVE_BUILD_STATUS: 'SUCCESS', NATIVE_BUILD_URL: 'https://jenkins.example/job/native/12/' });
  assertBase(body);
  assert.equal(body.nativeBuildUrl, 'https://jenkins.example/job/native/12/');
  assert.equal(body.nativeBuildStatus, 'SUCCESS');
});

test('report body: native build skipped -> valid JSON, base fields only', () => {
  const body = buildBody({ NATIVE_BUILD_STATUS: 'skipped', NATIVE_BUILD_URL: '' });
  assertBase(body);
  assert.deepEqual(Object.keys(body).sort(), ['buildIdentifier', 'candidateSha', 'previewUrl']);
});

test('report body: NATIVE_BUILD_STATUS unset -> valid JSON, base fields only', () => {
  const body = buildBody({});
  assertBase(body);
  assert.deepEqual(Object.keys(body).sort(), ['buildIdentifier', 'candidateSha', 'previewUrl']);
});

test("report body: today's behaviour, status 'unknown' with an empty URL still sends both native fields", () => {
  // Documents current behaviour only; whether this should send is an open human decision.
  const body = buildBody({ NATIVE_BUILD_STATUS: 'unknown', NATIVE_BUILD_URL: '' });
  assertBase(body);
  assert.equal(body.nativeBuildUrl, '');
  assert.equal(body.nativeBuildStatus, 'unknown');
});

test('report body key names match the body.get(...) calls in the core release-candidate view', () => {
  const call = VIEWS.match(/store\.report_release_candidate\(([\s\S]*?)origin=/);
  assert.ok(call, 'store.report_release_candidate call not found in views.py');
  const read = new Set([...call[1].matchAll(/body\.get\('(\w+)'\)/g)].map((m) => m[1]));
  assert.ok(VIEWS.includes("body['candidateSha']"), 'views.py no longer reads candidateSha');
  read.add('candidateSha');
  const sent = new Set(Object.keys(buildBody({ NATIVE_BUILD_STATUS: 'SUCCESS', NATIVE_BUILD_URL: 'u' })));
  assert.deepEqual([...sent].sort(), [...read].sort());
});
