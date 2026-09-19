'use strict';

// Container-side copy of services/scrummaster/src/envelope.js. Kept in setup/lib
// (mounted read-only into every project container at /agent-docs/lib) so
// subscriber.js and gateway-publish.js can require it without depending on
// the scrummaster package. Schema must stay identical to the ScrumMaster
// copy.

const crypto = require('crypto');

const SCHEMA_VERSION = '1';

const KIND = Object.freeze({
  TASK: 'task',
  TASK_STATUS: 'task_status',
  JIRA_OPERATION: 'jira_operation',
  WEBHOOK_EVENT: 'webhook_event',
});

const VALID_KINDS = new Set(Object.values(KIND));
const TASK_KINDS = new Set([KIND.TASK, KIND.TASK_STATUS]);

function newMessageId() {
  return `msg-${crypto.randomUUID()}`;
}

function buildEnvelope({ kind, project, taskId, contextId, correlationId, payload, messageId, createdAt }) {
  const envelope = {
    schemaVersion: SCHEMA_VERSION,
    messageId: messageId || newMessageId(),
    kind,
    project,
    taskId: taskId || null,
    contextId: contextId || null,
    correlationId: correlationId || null,
    createdAt: createdAt || new Date().toISOString(),
    payload: payload || {},
  };
  validateEnvelope(envelope);
  return envelope;
}

function validateEnvelope(envelope) {
  if (!envelope || typeof envelope !== 'object') {
    throw new Error('envelope must be an object');
  }
  if (envelope.schemaVersion !== SCHEMA_VERSION) {
    throw new Error(`envelope.schemaVersion must be "${SCHEMA_VERSION}", got ${JSON.stringify(envelope.schemaVersion)}`);
  }
  if (typeof envelope.messageId !== 'string' || envelope.messageId.length === 0) {
    throw new Error('envelope.messageId is required');
  }
  if (!VALID_KINDS.has(envelope.kind)) {
    throw new Error(`envelope.kind must be one of ${[...VALID_KINDS].join(', ')}, got ${JSON.stringify(envelope.kind)}`);
  }
  if (typeof envelope.project !== 'string' || envelope.project.length === 0) {
    throw new Error('envelope.project is required');
  }
  if (TASK_KINDS.has(envelope.kind)) {
    if (typeof envelope.taskId !== 'string' || envelope.taskId.length === 0) {
      throw new Error(`envelope.taskId is required for kind=${envelope.kind}`);
    }
    if (typeof envelope.contextId !== 'string' || envelope.contextId.length === 0) {
      throw new Error(`envelope.contextId is required for kind=${envelope.kind}`);
    }
  }
  if (typeof envelope.createdAt !== 'string' || Number.isNaN(Date.parse(envelope.createdAt))) {
    throw new Error('envelope.createdAt must be an RFC 3339 timestamp string');
  }
  if (envelope.payload === undefined || envelope.payload === null || typeof envelope.payload !== 'object') {
    throw new Error('envelope.payload must be an object');
  }
  return envelope;
}

function toStreamFields(envelope) {
  return { data: JSON.stringify(envelope) };
}

function fromStreamFields(fields) {
  try {
    const raw = fields && fields.data;
    if (typeof raw !== 'string') return null;
    const envelope = JSON.parse(raw);
    validateEnvelope(envelope);
    return envelope;
  } catch {
    return null;
  }
}

module.exports = {
  SCHEMA_VERSION,
  KIND,
  newMessageId,
  buildEnvelope,
  validateEnvelope,
  toStreamFields,
  fromStreamFields,
};
