'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { diffAgentFieldOptions } = require('../src/audit');

// The audit must identify each of the four drift
// classes independently and report ok:true only when none are present.

test('reports ok when Jira options exactly match the catalog', () => {
  const result = diffAgentFieldOptions(
    ['backend-agent', 'frontend-agent'],
    [],
    [
      { value: 'backend-agent', disabled: false },
      { value: 'frontend-agent', disabled: false },
    ]
  );
  assert.deepEqual(result, { missing: [], unexpected: [], mismatched: [], retiredButEnabled: [], ok: true });
});

test('reports a catalog id with no Jira option as missing', () => {
  const result = diffAgentFieldOptions(['backend-agent', 'devops-agent'], [], [
    { value: 'backend-agent', disabled: false },
  ]);
  assert.deepEqual(result.missing, ['devops-agent']);
  assert.equal(result.ok, false);
});

test('reports a Jira option with no catalog or retired backing as unexpected', () => {
  const result = diffAgentFieldOptions(['backend-agent'], [], [
    { value: 'backend-agent', disabled: false },
    { value: 'mystery-agent', disabled: false },
  ]);
  assert.deepEqual(result.unexpected, ['mystery-agent']);
  assert.equal(result.ok, false);
});

test('reports an active catalog id whose Jira option is disabled as mismatched', () => {
  const result = diffAgentFieldOptions(['backend-agent'], [], [
    { value: 'backend-agent', disabled: true },
  ]);
  assert.deepEqual(result.mismatched, ['backend-agent']);
  assert.equal(result.ok, false);
});

test('reports a retired id whose Jira option is still enabled as retired-but-enabled', () => {
  const result = diffAgentFieldOptions(['backend-agent'], ['qa-agent'], [
    { value: 'backend-agent', disabled: false },
    { value: 'qa-agent', disabled: false },
  ]);
  assert.deepEqual(result.retiredButEnabled, ['qa-agent']);
  assert.equal(result.ok, false);
});

test('a properly disabled retired option is not reported as drift', () => {
  const result = diffAgentFieldOptions(['backend-agent'], ['qa-agent'], [
    { value: 'backend-agent', disabled: false },
    { value: 'qa-agent', disabled: true },
  ]);
  assert.deepEqual(result, { missing: [], unexpected: [], mismatched: [], retiredButEnabled: [], ok: true });
});

test('all four drift classes can be reported together', () => {
  const result = diffAgentFieldOptions(
    ['backend-agent', 'devops-agent'],
    ['qa-agent'],
    [
      { value: 'backend-agent', disabled: true },   // mismatched
      { value: 'qa-agent', disabled: false },        // retired-but-enabled
      { value: 'mystery-agent', disabled: false },   // unexpected
      // devops-agent absent entirely                // missing
    ]
  );
  assert.deepEqual(result.missing, ['devops-agent']);
  assert.deepEqual(result.unexpected, ['mystery-agent']);
  assert.deepEqual(result.mismatched, ['backend-agent']);
  assert.deepEqual(result.retiredButEnabled, ['qa-agent']);
  assert.equal(result.ok, false);
});
