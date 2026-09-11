'use strict';

// messageId format ("msg-<uuid>") matches the A2A id convention
// (a2a-messaging.md) so the two features don't mint competing shapes. Now
// that a2a-messaging.md has landed, this repoints to the canonical generator
// rather than minting its own — see a2a/ids.js.
const { newMessageId } = require('./a2a/ids');

const SCHEMA_VERSION = '1';

const KIND = Object.freeze({
  TASK: 'task',
  TASK_STATUS: 'task_status',
  JIRA_OPERATION: 'jira_operation',
  WEBHOOK_EVENT: 'webhook_event',
  // the internal-work-item-service design REQ-03/REQ-05: a
  // command into, or an outbound change/rejection event out of, the
  // Internal Work-Item Service's canonical work-item store. Added for V2
  // rather than reusing WEBHOOK_EVENT/JIRA_OPERATION — neither name
  // describes a canonical work-item command/event, and this envelope
  // format (not a new one) is exactly what REQ-03 requires reusing.
  WORK_ITEM_COMMAND: 'work_item_command',
  WORK_ITEM_EVENT: 'work_item_event',
});

const VALID_KINDS = new Set(Object.values(KIND));

// Kinds that carry A2A task identity and therefore require taskId/contextId.
const TASK_KINDS = new Set([KIND.TASK, KIND.TASK_STATUS]);

// Build a schema-valid envelope. Throws if required fields are missing.
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

// Validate an envelope's shape. Throws a descriptive Error on the first
// violation found. Does not validate payload contents — payload shape is
// owned by the kind-specific handler, not the transport.
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

// Serialize an envelope into the field map used for XADD.
// A single 'data' field keeps the entry atomic to parse and matches the
// versioned-envelope-as-JSON-blob design in the feature spec.
function toStreamFields(envelope) {
  return { data: JSON.stringify(envelope) };
}

// Parse the field map returned by XREADGROUP/XRANGE back into an envelope.
// Returns null (never throws) so callers can route parse failures to the
// dead-letter path instead of crashing the consumer loop.
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
