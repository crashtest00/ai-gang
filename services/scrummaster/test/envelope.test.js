'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');

const { buildEnvelope, validateEnvelope, KIND, toStreamFields, fromStreamFields, SCHEMA_VERSION } = require('../src/envelope');
const {
  KIND: CONTAINER_SIDE_KIND,
  SCHEMA_VERSION: CONTAINER_SIDE_SCHEMA_VERSION,
  validateEnvelope: containerValidateEnvelope,
  fromStreamFields: containerFromStreamFields,
} = require('../../../setup/lib/envelope');

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

// V4 audit Pass 2 rows 34 and 36 — setup/lib/envelope.js's own header says
// "Schema must stay identical to the ScrumMaster copy". This proves the two
// KIND objects are identical, and that the V4 librarian pair validates as a
// genuine kind here too (VALID_KINDS membership, not just presence).
test('KIND is identical to setup/lib/envelope.js\'s container-side copy', () => {
  assert.deepStrictEqual(KIND, CONTAINER_SIDE_KIND);
});

test('ARTIFACT_DELIVERY_REQUEST/_RESPONSE match setup/lib/envelope.js\'s container-side copy byte-for-byte', () => {
  assert.equal(KIND.ARTIFACT_DELIVERY_REQUEST, 'artifact_delivery_request');
  assert.equal(KIND.ARTIFACT_DELIVERY_RESPONSE, 'artifact_delivery_response');
  assert.equal(KIND.ARTIFACT_DELIVERY_REQUEST, CONTAINER_SIDE_KIND.ARTIFACT_DELIVERY_REQUEST);
  assert.equal(KIND.ARTIFACT_DELIVERY_RESPONSE, CONTAINER_SIDE_KIND.ARTIFACT_DELIVERY_RESPONSE);
  assert.doesNotThrow(() => buildEnvelope({ kind: KIND.ARTIFACT_DELIVERY_REQUEST, project: 'p', payload: {} }));
  assert.doesNotThrow(() => buildEnvelope({ kind: KIND.ARTIFACT_DELIVERY_RESPONSE, project: 'p', payload: {} }));
});

// V4 audit Pass 3 row 41 — setup/lib/envelope.js:6 claims the whole schema
// is identical to this copy, but until now only KIND was pinned.
// SCHEMA_VERSION and the accept/reject behavior of validateEnvelope and
// fromStreamFields were unpinned and could silently drift.
test('SCHEMA_VERSION is identical to setup/lib/envelope.js\'s container-side copy', () => {
  assert.equal(SCHEMA_VERSION, CONTAINER_SIDE_SCHEMA_VERSION);
});

const ENVELOPE_TABLE_NOW = new Date().toISOString();

// One shared table of valid and invalid envelopes, run through both
// copies' validateEnvelope and fromStreamFields, so a future edit to
// either copy's validation logic that the two disagree on fails here
// instead of only showing up as a runtime dead-letter mismatch.
const ENVELOPE_TABLE = [
  {
    name: 'valid jira_operation envelope',
    valid: true,
    envelope: { schemaVersion: '1', messageId: 'm-1', kind: KIND.JIRA_OPERATION, project: 'p', taskId: null, contextId: null, correlationId: null, createdAt: ENVELOPE_TABLE_NOW, payload: { type: 'comment' } },
  },
  {
    name: 'valid task envelope with taskId/contextId',
    valid: true,
    envelope: { schemaVersion: '1', messageId: 'm-2', kind: KIND.TASK, project: 'p', taskId: 'T-1', contextId: 'T-1', correlationId: null, createdAt: ENVELOPE_TABLE_NOW, payload: {} },
  },
  {
    name: 'valid artifact_delivery_request envelope',
    valid: true,
    envelope: { schemaVersion: '1', messageId: 'm-3', kind: KIND.ARTIFACT_DELIVERY_REQUEST, project: 'p', taskId: null, contextId: null, correlationId: null, createdAt: ENVELOPE_TABLE_NOW, payload: {} },
  },
  {
    name: 'valid artifact_delivery_response envelope',
    valid: true,
    envelope: { schemaVersion: '1', messageId: 'm-4', kind: KIND.ARTIFACT_DELIVERY_RESPONSE, project: 'p', taskId: null, contextId: null, correlationId: null, createdAt: ENVELOPE_TABLE_NOW, payload: {} },
  },
  {
    name: 'task kind missing taskId',
    valid: false,
    envelope: { schemaVersion: '1', messageId: 'm-5', kind: KIND.TASK, project: 'p', taskId: null, contextId: null, correlationId: null, createdAt: ENVELOPE_TABLE_NOW, payload: {} },
  },
  {
    name: 'task_status kind missing contextId',
    valid: false,
    envelope: { schemaVersion: '1', messageId: 'm-6', kind: KIND.TASK_STATUS, project: 'p', taskId: 'T-1', contextId: null, correlationId: null, createdAt: ENVELOPE_TABLE_NOW, payload: {} },
  },
  {
    name: 'unknown kind',
    valid: false,
    envelope: { schemaVersion: '1', messageId: 'm-7', kind: 'bogus', project: 'p', taskId: null, contextId: null, correlationId: null, createdAt: ENVELOPE_TABLE_NOW, payload: {} },
  },
  {
    name: 'wrong schemaVersion',
    valid: false,
    envelope: { schemaVersion: '2', messageId: 'm-8', kind: KIND.JIRA_OPERATION, project: 'p', taskId: null, contextId: null, correlationId: null, createdAt: ENVELOPE_TABLE_NOW, payload: {} },
  },
  {
    name: 'empty messageId',
    valid: false,
    envelope: { schemaVersion: '1', messageId: '', kind: KIND.JIRA_OPERATION, project: 'p', taskId: null, contextId: null, correlationId: null, createdAt: ENVELOPE_TABLE_NOW, payload: {} },
  },
  {
    name: 'empty project',
    valid: false,
    envelope: { schemaVersion: '1', messageId: 'm-9', kind: KIND.JIRA_OPERATION, project: '', taskId: null, contextId: null, correlationId: null, createdAt: ENVELOPE_TABLE_NOW, payload: {} },
  },
  {
    name: 'unparseable createdAt',
    valid: false,
    envelope: { schemaVersion: '1', messageId: 'm-10', kind: KIND.JIRA_OPERATION, project: 'p', taskId: null, contextId: null, correlationId: null, createdAt: 'not-a-date', payload: {} },
  },
  {
    name: 'null payload',
    valid: false,
    envelope: { schemaVersion: '1', messageId: 'm-11', kind: KIND.JIRA_OPERATION, project: 'p', taskId: null, contextId: null, correlationId: null, createdAt: ENVELOPE_TABLE_NOW, payload: null },
  },
];

test('validateEnvelope and fromStreamFields agree between the two envelope copies on a shared table of valid and invalid envelopes', () => {
  const accepts = (validateFn, envelope) => {
    try {
      validateFn(envelope);
      return true;
    } catch {
      return false;
    }
  };

  for (const { name, valid, envelope } of ENVELOPE_TABLE) {
    const scrummasterValidates = accepts(validateEnvelope, envelope);
    const containerValidates = accepts(containerValidateEnvelope, envelope);
    assert.equal(scrummasterValidates, valid, `ScrumMaster validateEnvelope outcome for: ${name}`);
    assert.equal(containerValidates, valid, `container validateEnvelope outcome for: ${name}`);

    const fields = { data: JSON.stringify(envelope) };
    const scrummasterParsed = fromStreamFields(fields);
    const containerParsed = containerFromStreamFields(fields);
    assert.equal(scrummasterParsed !== null, valid, `ScrumMaster fromStreamFields outcome for: ${name}`);
    assert.equal(containerParsed !== null, valid, `container fromStreamFields outcome for: ${name}`);
    assert.deepEqual(scrummasterParsed, containerParsed, `fromStreamFields result mismatch for: ${name}`);
  }
});
