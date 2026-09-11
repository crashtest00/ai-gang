'use strict';

// Pure builders for A2A-shaped objects. These do not validate or store
// anything — schema.js validates, taskStore.js stores and enforces lifecycle.

function buildTextPart(text) {
  return { kind: 'text', text };
}

function buildDataPart(data) {
  return { kind: 'data', data };
}

function buildFilePart({ name, mimeType, uri, bytes }) {
  const file = {};
  if (name) file.name = name;
  if (mimeType) file.mimeType = mimeType;
  if (uri) file.uri = uri;
  if (bytes) file.bytes = bytes;
  return { kind: 'file', file };
}

function buildMessage({ messageId, taskId, contextId, role, parts, referenceMessageId, metadata, timestamp }) {
  const message = {
    kind: 'message',
    messageId,
    taskId,
    contextId,
    role,
    parts,
    timestamp: timestamp || new Date().toISOString(),
  };
  if (referenceMessageId) message.referenceMessageId = referenceMessageId;
  if (metadata) message.metadata = metadata;
  return message;
}

function buildArtifact({ artifactId, taskId, name, parts, referenceArtifactId, metadata, timestamp }) {
  const artifact = {
    kind: 'artifact',
    artifactId,
    taskId,
    name,
    parts,
    timestamp: timestamp || new Date().toISOString(),
  };
  if (referenceArtifactId) artifact.referenceArtifactId = referenceArtifactId;
  if (metadata) artifact.metadata = metadata;
  return artifact;
}

function buildTask({ id, contextId, status, metadata }) {
  const task = { kind: 'task', id, contextId, status };
  if (metadata) task.metadata = metadata;
  return task;
}

module.exports = {
  buildTextPart,
  buildDataPart,
  buildFilePart,
  buildMessage,
  buildArtifact,
  buildTask,
};
