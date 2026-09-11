'use strict';

// Canonical A2A-aligned schema validation for AI Gang.
//
// This is a hand-rolled validator rather than a JSON Schema library: the
// object model is small and fixed (Part, Message, Task, Artifact, AgentCard,
// the pub/sub envelope), and strict "no unknown top-level keys" checking
// keeps routing/integration data confined to `metadata`, never invented as
// a sibling top-level field.

class A2AValidationError extends Error {
  constructor(objectKind, errors) {
    super(`Invalid ${objectKind}: ${errors.join('; ')}`);
    this.name = 'A2AValidationError';
    this.objectKind = objectKind;
    this.errors = errors;
  }
}

const TASK_STATES = Object.freeze([
  'submitted', 'working', 'input-required', 'auth-required',
  'completed', 'failed', 'canceled', 'rejected',
]);

const TERMINAL_STATES = Object.freeze(['completed', 'failed', 'canceled', 'rejected']);
const INTERRUPTED_STATES = Object.freeze(['input-required', 'auth-required']);

const MESSAGE_ROLES = Object.freeze(['client', 'agent']);
const PART_KINDS = Object.freeze(['text', 'data', 'file']);

function isPlainObject(v) {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

function isNonEmptyString(v) {
  return typeof v === 'string' && v.length > 0;
}

// Collect keys present on `obj` that are not in `allowed`.
function unknownKeys(obj, allowed) {
  return Object.keys(obj).filter(k => !allowed.includes(k));
}

function validatePart(part, errors, path) {
  if (!isPlainObject(part)) { errors.push(`${path} must be an object`); return; }
  if (!PART_KINDS.includes(part.kind)) {
    errors.push(`${path}.kind must be one of ${PART_KINDS.join('/')}`);
    return;
  }

  if (part.kind === 'text') {
    const extra = unknownKeys(part, ['kind', 'text']);
    if (extra.length) errors.push(`${path} has unsupported keys: ${extra.join(', ')}`);
    if (!isNonEmptyString(part.text)) errors.push(`${path}.text must be a non-empty string`);
    return;
  }

  if (part.kind === 'data') {
    const extra = unknownKeys(part, ['kind', 'data']);
    if (extra.length) errors.push(`${path} has unsupported keys: ${extra.join(', ')}`);
    if (!isPlainObject(part.data)) errors.push(`${path}.data must be an object`);
    return;
  }

  // file
  const extra = unknownKeys(part, ['kind', 'file']);
  if (extra.length) errors.push(`${path} has unsupported keys: ${extra.join(', ')}`);
  if (!isPlainObject(part.file)) {
    errors.push(`${path}.file must be an object`);
    return;
  }
  const fileExtra = unknownKeys(part.file, ['name', 'mimeType', 'uri', 'bytes']);
  if (fileExtra.length) errors.push(`${path}.file has unsupported keys: ${fileExtra.join(', ')}`);
  if (!part.file.uri && !part.file.bytes) {
    errors.push(`${path}.file must have a "uri" reference or inline "bytes"`);
  }
}

function validateParts(parts, errors, path) {
  if (!Array.isArray(parts) || parts.length === 0) {
    errors.push(`${path} must be a non-empty array of Parts`);
    return;
  }
  parts.forEach((p, i) => validatePart(p, errors, `${path}[${i}]`));
}

const MESSAGE_TOP_KEYS = ['kind', 'messageId', 'taskId', 'contextId', 'role', 'parts', 'referenceMessageId', 'metadata', 'timestamp'];

function validateMessage(message, errors, path = 'message') {
  if (!isPlainObject(message)) { errors.push(`${path} must be an object`); return; }

  const extra = unknownKeys(message, MESSAGE_TOP_KEYS);
  if (extra.length) errors.push(`${path} has unsupported top-level keys (move to metadata): ${extra.join(', ')}`);

  if (message.kind !== 'message') errors.push(`${path}.kind must be "message"`);
  if (!isNonEmptyString(message.messageId)) errors.push(`${path}.messageId must be a non-empty string`);
  if (!isNonEmptyString(message.taskId)) errors.push(`${path}.taskId must be a non-empty string`);
  if (!isNonEmptyString(message.contextId)) errors.push(`${path}.contextId must be a non-empty string`);
  if (!MESSAGE_ROLES.includes(message.role)) errors.push(`${path}.role must be one of ${MESSAGE_ROLES.join('/')}`);
  if (message.referenceMessageId !== undefined && message.referenceMessageId !== null && !isNonEmptyString(message.referenceMessageId)) {
    errors.push(`${path}.referenceMessageId must be a string when present`);
  }
  if (message.metadata !== undefined && !isPlainObject(message.metadata)) {
    errors.push(`${path}.metadata must be an object when present`);
  }
  validateParts(message.parts, errors, `${path}.parts`);
}

const ARTIFACT_TOP_KEYS = ['kind', 'artifactId', 'taskId', 'name', 'parts', 'referenceArtifactId', 'metadata', 'timestamp'];

function validateArtifact(artifact, errors, path = 'artifact') {
  if (!isPlainObject(artifact)) { errors.push(`${path} must be an object`); return; }

  const extra = unknownKeys(artifact, ARTIFACT_TOP_KEYS);
  if (extra.length) errors.push(`${path} has unsupported top-level keys (move to metadata): ${extra.join(', ')}`);

  if (artifact.kind !== 'artifact') errors.push(`${path}.kind must be "artifact"`);
  if (!isNonEmptyString(artifact.artifactId)) errors.push(`${path}.artifactId must be a non-empty string`);
  if (!isNonEmptyString(artifact.taskId)) errors.push(`${path}.taskId must be a non-empty string`);
  if (!isNonEmptyString(artifact.name)) errors.push(`${path}.name must be a non-empty string`);
  if (artifact.referenceArtifactId !== undefined && artifact.referenceArtifactId !== null && !isNonEmptyString(artifact.referenceArtifactId)) {
    errors.push(`${path}.referenceArtifactId must be a string when present`);
  }
  if (artifact.metadata !== undefined && !isPlainObject(artifact.metadata)) {
    errors.push(`${path}.metadata must be an object when present`);
  }
  validateParts(artifact.parts, errors, `${path}.parts`);
}

const TASK_TOP_KEYS = ['kind', 'id', 'contextId', 'status', 'metadata'];
const STATUS_KEYS = ['state', 'timestamp', 'message'];

function validateTask(task, errors, path = 'task') {
  if (!isPlainObject(task)) { errors.push(`${path} must be an object`); return; }

  const extra = unknownKeys(task, TASK_TOP_KEYS);
  if (extra.length) errors.push(`${path} has unsupported top-level keys (move to metadata): ${extra.join(', ')}`);

  if (task.kind !== 'task') errors.push(`${path}.kind must be "task"`);
  if (!isNonEmptyString(task.id)) errors.push(`${path}.id must be a non-empty string`);
  if (!isNonEmptyString(task.contextId)) errors.push(`${path}.contextId must be a non-empty string`);
  if (task.metadata !== undefined && !isPlainObject(task.metadata)) {
    errors.push(`${path}.metadata must be an object when present`);
  }

  if (!isPlainObject(task.status)) {
    errors.push(`${path}.status must be an object`);
    return;
  }
  const statusExtra = unknownKeys(task.status, STATUS_KEYS);
  if (statusExtra.length) errors.push(`${path}.status has unsupported keys: ${statusExtra.join(', ')}`);
  if (!TASK_STATES.includes(task.status.state)) {
    errors.push(`${path}.status.state must be one of ${TASK_STATES.join('/')}`);
  }
  if (!isNonEmptyString(task.status.timestamp)) {
    errors.push(`${path}.status.timestamp must be a non-empty ISO-8601 string`);
  }
  if (task.status.message !== undefined && task.status.message !== null) {
    validateMessage(task.status.message, errors, `${path}.status.message`);
  }
}

const ENVELOPE_TOP_KEYS = ['schemaVersion', 'task', 'artifacts'];

function validateEnvelopeShape(envelope, errors, path = 'envelope') {
  if (!isPlainObject(envelope)) { errors.push(`${path} must be an object`); return; }

  const extra = unknownKeys(envelope, ENVELOPE_TOP_KEYS);
  if (extra.length) errors.push(`${path} has unsupported top-level keys: ${extra.join(', ')}`);

  if (envelope.schemaVersion !== '1') errors.push(`${path}.schemaVersion must be "1"`);
  validateTask(envelope.task, errors, `${path}.task`);

  if (envelope.artifacts !== undefined) {
    if (!Array.isArray(envelope.artifacts)) {
      errors.push(`${path}.artifacts must be an array when present`);
    } else {
      envelope.artifacts.forEach((a, i) => validateArtifact(a, errors, `${path}.artifacts[${i}]`));
    }
  }
}

const AGENT_CARD_KEYS = ['name', 'description', 'provider', 'version', 'protocolVersion', 'capabilities', 'skills', 'defaultInputModes', 'defaultOutputModes'];

function validateAgentCard(card, errors, path = 'agentCard') {
  if (!isPlainObject(card)) { errors.push(`${path} must be an object`); return; }

  const extra = unknownKeys(card, AGENT_CARD_KEYS);
  if (extra.length) errors.push(`${path} has unsupported keys: ${extra.join(', ')}`);

  if (!isNonEmptyString(card.name)) errors.push(`${path}.name must be a non-empty string`);
  if (!isNonEmptyString(card.description)) errors.push(`${path}.description must be a non-empty string`);
  if (!isPlainObject(card.provider)) errors.push(`${path}.provider must be an object`);
  if (!isNonEmptyString(card.version)) errors.push(`${path}.version must be a non-empty string`);
  if (!isNonEmptyString(card.protocolVersion)) errors.push(`${path}.protocolVersion must be a non-empty string`);
  if (!isPlainObject(card.capabilities)) errors.push(`${path}.capabilities must be an object`);

  if (!Array.isArray(card.skills) || card.skills.length === 0) {
    errors.push(`${path}.skills must be a non-empty array — an AgentCard must advertise at least one caller-facing skill`);
  } else {
    card.skills.forEach((skill, i) => {
      if (!isPlainObject(skill)) { errors.push(`${path}.skills[${i}] must be an object`); return; }
      const skillExtra = unknownKeys(skill, ['id', 'name', 'description']);
      if (skillExtra.length) errors.push(`${path}.skills[${i}] has unsupported keys: ${skillExtra.join(', ')}`);
      if (!isNonEmptyString(skill.id)) errors.push(`${path}.skills[${i}].id must be a non-empty string`);
      if (!isNonEmptyString(skill.name)) errors.push(`${path}.skills[${i}].name must be a non-empty string`);
      if (!isNonEmptyString(skill.description)) errors.push(`${path}.skills[${i}].description must be a non-empty string`);
    });
  }

  if (!Array.isArray(card.defaultInputModes) || card.defaultInputModes.some(m => !isNonEmptyString(m))) {
    errors.push(`${path}.defaultInputModes must be an array of non-empty strings`);
  }
  if (!Array.isArray(card.defaultOutputModes) || card.defaultOutputModes.some(m => !isNonEmptyString(m))) {
    errors.push(`${path}.defaultOutputModes must be an array of non-empty strings`);
  }
}

// Each `validateX` throws A2AValidationError on failure, returns the validated
// object unchanged on success (so callers can chain: `const t = validateTask(x)`).
function assert(validateFn, kind) {
  return (obj, ...rest) => {
    const errors = [];
    validateFn(obj, errors, ...rest);
    if (errors.length) throw new A2AValidationError(kind, errors);
    return obj;
  };
}

module.exports = {
  A2AValidationError,
  TASK_STATES,
  TERMINAL_STATES,
  INTERRUPTED_STATES,
  MESSAGE_ROLES,
  PART_KINDS,
  validatePart: assert(validatePart, 'Part'),
  validateMessage: assert(validateMessage, 'Message'),
  validateArtifact: assert(validateArtifact, 'Artifact'),
  validateTask: assert(validateTask, 'Task'),
  validateEnvelope: assert(validateEnvelopeShape, 'Envelope'),
  validateAgentCard: assert(validateAgentCard, 'AgentCard'),
};
