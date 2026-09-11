'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const schema = require('./schema');
const { buildTextPart, buildDataPart, buildFilePart, buildMessage, buildArtifact, buildTask } = require('./parts');

function baseMessage(overrides = {}) {
  return buildMessage({
    messageId: 'msg-1',
    taskId: 'task-1',
    contextId: 'ctx-1',
    role: 'client',
    parts: [buildTextPart('hello')],
    ...overrides,
  });
}

// REQ-05 — Parts and artifacts

test('validatePart accepts text, data, and file parts', () => {
  schema.validatePart(buildTextPart('hi'));
  schema.validatePart(buildDataPart({ operation: 'comment' }));
  schema.validatePart(buildFilePart({ uri: 'https://example.com/x' }));
  schema.validatePart(buildFilePart({ bytes: 'aGVsbG8=' }));
});

test('validatePart rejects an unsupported part shape', () => {
  assert.throws(() => schema.validatePart({ kind: 'video', url: 'x' }), schema.A2AValidationError);
});

test('validatePart rejects a file part with neither uri nor bytes', () => {
  assert.throws(() => schema.validatePart({ kind: 'file', file: { name: 'x' } }), schema.A2AValidationError);
});

test('validateMessage requires a non-empty parts array', () => {
  assert.throws(() => schema.validateMessage(baseMessage({ parts: [] })), schema.A2AValidationError);
});

// REQ-03 — symmetric content model, no from/to, role required

test('validateMessage accepts both client and agent roles with the same shape', () => {
  schema.validateMessage(baseMessage({ role: 'client' }));
  schema.validateMessage(baseMessage({ role: 'agent' }));
});

test('validateMessage rejects an unknown role', () => {
  assert.throws(() => schema.validateMessage(baseMessage({ role: 'server' })), schema.A2AValidationError);
});

test('validateMessage rejects "from"/"to" as top-level content fields', () => {
  const message = baseMessage();
  message.from = 'backend-agent';
  assert.throws(() => schema.validateMessage(message), schema.A2AValidationError);

  const message2 = baseMessage();
  message2.to = 'scrummaster';
  assert.throws(() => schema.validateMessage(message2), schema.A2AValidationError);
});

// REQ-04 — AI Gang integration metadata must live in `metadata`

test('validateTask accepts a Jira issue key inside metadata', () => {
  schema.validateTask(buildTask({
    id: 'task-1',
    contextId: 'ctx-1',
    status: { state: 'submitted', timestamp: new Date().toISOString(), message: baseMessage() },
    metadata: { jiraIssueKey: 'GANG-42' },
  }));
});

test('validateTask rejects a Jira issue key at the top level', () => {
  const task = buildTask({
    id: 'task-1',
    contextId: 'ctx-1',
    status: { state: 'submitted', timestamp: new Date().toISOString(), message: baseMessage() },
  });
  task.jiraIssueKey = 'GANG-42';
  assert.throws(() => schema.validateTask(task), schema.A2AValidationError);
});

// REQ-02 — lifecycle states

test('validateTask accepts every adopted lifecycle state', () => {
  for (const state of schema.TASK_STATES) {
    schema.validateTask(buildTask({
      id: 'task-1',
      contextId: 'ctx-1',
      status: { state, timestamp: new Date().toISOString() },
    }));
  }
});

test('validateTask rejects an unknown lifecycle state', () => {
  assert.throws(() => schema.validateTask(buildTask({
    id: 'task-1',
    contextId: 'ctx-1',
    status: { state: 'in-progress', timestamp: new Date().toISOString() },
  })), schema.A2AValidationError);
});

// Artifacts — human-readable + machine-readable parts together

test('validateArtifact accepts a file part and a text part together', () => {
  schema.validateArtifact(buildArtifact({
    artifactId: 'artifact-1',
    taskId: 'task-1',
    name: 'pull-request',
    parts: [
      buildFilePart({ name: 'pr', mimeType: 'text/uri-list', uri: 'https://example.com/pr/1' }),
      buildTextPart('Implements the thing'),
    ],
  }));
});

// REQ-08 — AgentCard

test('validateAgentCard requires at least one advertised skill', () => {
  assert.throws(() => schema.validateAgentCard({
    name: 'Backend Agent',
    description: 'desc',
    provider: {},
    version: '1.0.0',
    protocolVersion: '0.2',
    capabilities: {},
    skills: [],
    defaultInputModes: ['text/plain'],
    defaultOutputModes: ['text/plain'],
  }), schema.A2AValidationError);
});

test('validateAgentCard accepts a complete card', () => {
  schema.validateAgentCard({
    name: 'Backend Agent',
    description: 'desc',
    provider: { organization: 'AI Gang' },
    version: '1.0.0',
    protocolVersion: '0.2',
    capabilities: { streaming: false },
    skills: [{ id: 'implement-backend-change', name: 'Implement backend change', description: 'desc' }],
    defaultInputModes: ['text/plain'],
    defaultOutputModes: ['text/plain'],
  });
});

// REQ-01 — envelope wraps the canonical objects, nothing else

test('validateEnvelope rejects an unknown top-level key', () => {
  const envelope = {
    schemaVersion: '1',
    task: buildTask({
      id: 'task-1',
      contextId: 'ctx-1',
      status: { state: 'submitted', timestamp: new Date().toISOString(), message: baseMessage() },
    }),
    prompt: 'legacy field should not be accepted',
  };
  assert.throws(() => schema.validateEnvelope(envelope), schema.A2AValidationError);
});

test('validateEnvelope accepts a task with artifacts', () => {
  const task = buildTask({
    id: 'task-1',
    contextId: 'ctx-1',
    status: { state: 'completed', timestamp: new Date().toISOString(), message: baseMessage({ role: 'agent' }) },
  });
  const artifact = buildArtifact({ artifactId: 'artifact-1', taskId: 'task-1', name: 'pull-request', parts: [buildTextPart('done')] });
  schema.validateEnvelope({ schemaVersion: '1', task, artifacts: [artifact] });
});
