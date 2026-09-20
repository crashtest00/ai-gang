'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const { findDuplicateKeyPaths } = require('../lib/config/duplicate-keys');

test('findDuplicateKeyPaths: no duplicates in a well-formed document', () => {
  const text = JSON.stringify({ schemaVersion: 1, project: { name: 'a', type: 'web', stack: 's' } });
  assert.deepEqual(findDuplicateKeyPaths(text), []);
});

test('findDuplicateKeyPaths: a duplicate top-level key is reported', () => {
  const text = '{"schemaVersion":1,"schemaVersion":2,"project":{}}';
  assert.deepEqual(findDuplicateKeyPaths(text), ['schemaVersion']);
});

test('findDuplicateKeyPaths: a duplicate nested key is reported with its path', () => {
  const text = '{"schemaVersion":1,"project":{"name":"a","name":"b","type":"web","stack":"s"}}';
  assert.deepEqual(findDuplicateKeyPaths(text), ['project.name']);
});

test('findDuplicateKeyPaths: same key name in two different sibling objects is not a duplicate', () => {
  const text = '{"a":{"name":"x"},"b":{"name":"y"}}';
  assert.deepEqual(findDuplicateKeyPaths(text), []);
});

test('findDuplicateKeyPaths: three occurrences of one key report two duplicates', () => {
  const text = '{"x":1,"x":2,"x":3}';
  assert.deepEqual(findDuplicateKeyPaths(text), ['x', 'x']);
});
