'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');

const { buildEnvelope, validateEnvelope, KIND, toStreamFields, fromStreamFields } = require('../src/envelope');
const { KIND: CONTAINER_SIDE_KIND } = require('../../../setup/lib/envelope');

test('buildEnvelope produces a schema-valid envelope with defaults', () => {
  const envelope = buildEnvelope({ kind: KIND.JIRA_OPERATION, project: 'hello-world', payload: { type: 'comment' } });
  assert.equal(envelope.schemaVersion, '1');
  assert.match(envelope.messageId, /^msg-/);
  assert.equal(envelope.taskId, null);
  assert.equal(envelope.contextId, null);
  assert.ok(!Number.isNaN(Date.parse(envelope.createdAt)));
});

test('task and task_status kinds require taskId and contextId', () => {
  assert.throws(() => buildEnvelope({ kind: KIND.TASK, project: 'p', payload: {} }), /taskId is required/);
  assert.doesNotThrow(() =>
    buildEnvelope({ kind: KIND.TASK, project: 'p', taskId: 'T-1', contextId: 'T-1', payload: {} })
  );
});

test('validateEnvelope rejects an unknown kind', () => {
  assert.throws(
    () => validateEnvelope({ schemaVersion: '1', messageId: 'm', kind: 'bogus', project: 'p', createdAt: new Date().toISOString(), payload: {} }),
    /kind must be one of/
  );
});

test('validateEnvelope rejects a wrong schemaVersion', () => {
  assert.throws(
    () => validateEnvelope({ schemaVersion: '2', messageId: 'm', kind: KIND.JIRA_OPERATION, project: 'p', createdAt: new Date().toISOString(), payload: {} }),
    /schemaVersion/
  );
});

test('toStreamFields/fromStreamFields round-trip', () => {
  const envelope = buildEnvelope({ kind: KIND.JIRA_OPERATION, project: 'p', payload: { type: 'comment', body: 'hi' } });
  const fields = toStreamFields(envelope);
  assert.deepEqual(Object.keys(fields), ['data']);
  const parsed = fromStreamFields(fields);
  assert.deepEqual(parsed, envelope);
});

test('fromStreamFields returns null instead of throwing on garbage', () => {
  assert.equal(fromStreamFields({ data: 'not json' }), null);
  assert.equal(fromStreamFields({ data: JSON.stringify({ schemaVersion: '1' }) }), null); // missing required fields
  assert.equal(fromStreamFields({}), null);
});

// V4 audit Pass 2 row 34 — setup/lib/envelope.js's own header says "Schema
// must stay identical to the ScrumMaster copy". PR #21 added
// artifact_delivery_request/_response to only the container-side copy;
// this proves the pair now matches byte-for-byte and is a genuine valid
// kind here too (VALID_KINDS membership, not just presence in the KIND
// object), rather than trusting the comment.
//
// Scoped to this one pair, not a full KIND deep-equal: the two files also
// diverge on WORK_ITEM_COMMAND/WORK_ITEM_EVENT (present in this copy,
// absent from setup/lib/envelope.js since that file's initial import,
// unrelated to row 34 or PR #21) — a separate, pre-existing gap outside
// this fix's file ownership; see the row 34 fix's report.
test('ARTIFACT_DELIVERY_REQUEST/_RESPONSE match setup/lib/envelope.js\'s container-side copy byte-for-byte', () => {
  assert.equal(KIND.ARTIFACT_DELIVERY_REQUEST, 'artifact_delivery_request');
  assert.equal(KIND.ARTIFACT_DELIVERY_RESPONSE, 'artifact_delivery_response');
  assert.equal(KIND.ARTIFACT_DELIVERY_REQUEST, CONTAINER_SIDE_KIND.ARTIFACT_DELIVERY_REQUEST);
  assert.equal(KIND.ARTIFACT_DELIVERY_RESPONSE, CONTAINER_SIDE_KIND.ARTIFACT_DELIVERY_RESPONSE);
  assert.doesNotThrow(() => buildEnvelope({ kind: KIND.ARTIFACT_DELIVERY_REQUEST, project: 'p', payload: {} }));
  assert.doesNotThrow(() => buildEnvelope({ kind: KIND.ARTIFACT_DELIVERY_RESPONSE, project: 'p', payload: {} }));
});
