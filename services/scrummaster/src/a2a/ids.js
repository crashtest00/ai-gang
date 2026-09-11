'use strict';

const crypto = require('crypto');

function newId(prefix) {
  return `${prefix}-${crypto.randomUUID()}`;
}

module.exports = {
  newTaskId: () => newId('task'),
  newContextId: () => newId('ctx'),
  newMessageId: () => newId('msg'),
  newArtifactId: () => newId('artifact'),
};
