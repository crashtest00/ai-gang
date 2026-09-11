'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const { walkGraph } = require('../lib/walker');

function decisionDoc() {
  return {
    graph_id: 'g',
    schema_version: 1,
    entry: 'check',
    nodes: [
      {
        id: 'check',
        kind: 'decision',
        check: { description: 'd', probe: 'p', error_when: 'probe-error' },
        branches: [
          { when: 'yes', to: 'do-it' },
          { when: 'no', to: 'remediate' },
          { when: 'probe-error', to: 'remediate' },
        ],
      },
      { id: 'do-it', kind: 'action', procedure: 'run it', next: 'done' },
      { id: 'remediate', kind: 'remediation', guidance: '[HUMAN] fix it', next: 'check' },
      { id: 'done', kind: 'action', procedure: 'no-op', terminal: true, outcome: 'success' },
    ],
  };
}

test('walks a decision -> action -> terminal path and records a full transcript', async () => {
  const doc = decisionDoc();
  const result = await walkGraph(doc, { evaluateCheck: async () => 'yes' });
  assert.equal(result.outcome, 'success');
  const refs = result.transcript.map((t) => t.ref);
  assert.deepEqual(refs, ['g#check:yes', 'g#do-it:next', 'g#done']);
});

test('single-node-at-a-time (REQ-07): the walker never records more than one "current" node per step', async () => {
  const doc = decisionDoc();
  const seenConcurrently = [];
  let inFlight = 0;
  const result = await walkGraph(doc, {
    evaluateCheck: async () => {
      inFlight++;
      seenConcurrently.push(inFlight);
      await new Promise((r) => setTimeout(r, 1));
      inFlight--;
      return 'yes';
    },
  });
  assert.deepEqual(seenConcurrently, [1]);
  assert.equal(result.outcome, 'success');
});

test('a remediation node loops back to re-verify the same decision, then proceeds once fixed', async () => {
  const doc = decisionDoc();
  let attempts = 0;
  const result = await walkGraph(doc, {
    evaluateCheck: async () => {
      attempts++;
      return attempts === 1 ? 'no' : 'yes';
    },
  });
  assert.equal(result.outcome, 'success');
  const refs = result.transcript.map((t) => t.ref);
  assert.deepEqual(refs, ['g#check:no', 'g#remediate:next', 'g#check:yes', 'g#do-it:next', 'g#done']);
});

test('an unmatched decision outcome throws rather than silently continuing', async () => {
  const doc = decisionDoc();
  await assert.rejects(
    () => walkGraph(doc, { evaluateCheck: async () => 'not-a-declared-outcome' }),
    /no matching branch/
  );
});

test('runAction and onRemediation handlers are invoked with the node', async () => {
  const doc = decisionDoc();
  const actionsRun = [];
  const remediationsRun = [];
  let attempts = 0;
  await walkGraph(doc, {
    evaluateCheck: async () => {
      attempts++;
      return attempts === 1 ? 'no' : 'yes';
    },
    runAction: async (node) => actionsRun.push(node.id),
    onRemediation: async (node) => {
      remediationsRun.push(node.id);
    },
  });
  assert.deepEqual(remediationsRun, ['remediate']);
  assert.deepEqual(actionsRun, ['do-it']);
});

test('an unresolved loop is bounded by maxSteps rather than hanging forever', async () => {
  const doc = decisionDoc();
  await assert.rejects(
    () => walkGraph(doc, { evaluateCheck: async () => 'no' }, { maxSteps: 10 }),
    /exceeded maxSteps/
  );
});

// --- fan-out sequential fallback (REQ-16) ---

function fanOutDoc() {
  return {
    graph_id: 'g',
    schema_version: 1,
    entry: 'split',
    nodes: [
      {
        id: 'split',
        kind: 'fan-out',
        branches: [
          { branch_id: 'left', to: 'left-work' },
          { branch_id: 'right', to: 'right-work' },
        ],
        join: 'joined',
      },
      { id: 'left-work', kind: 'action', procedure: 'left', next: 'joined' },
      { id: 'right-work', kind: 'action', procedure: 'right', next: 'joined' },
      { id: 'joined', kind: 'fan-in', for: 'split', next: 'done' },
      { id: 'done', kind: 'action', procedure: 'no-op', terminal: true, outcome: 'success' },
    ],
  };
}

test('fan-out fallback (no coordination mechanism): branches walk sequentially, in listing order', async () => {
  const doc = fanOutDoc();
  const order = [];
  const result = await walkGraph(doc, {
    runAction: async (node) => {
      order.push(node.id);
    },
  });
  assert.equal(result.outcome, 'success');
  assert.deepEqual(order, ['left-work', 'right-work']);
  const fanOutEntry = result.transcript.find((t) => t.event === 'fan-out-sequential-fallback');
  assert.ok(fanOutEntry);
  assert.deepEqual(
    fanOutEntry.branches.map((b) => b.branch_id),
    ['left', 'right']
  );
});

test('fan-out fallback never advances to fan-in before every branch has completed', async () => {
  const doc = fanOutDoc();
  const order = [];
  await walkGraph(doc, {
    runAction: async (node) => {
      order.push(node.id);
    },
  });
  const joinedIndex = order.indexOf('right-work'); // last branch action
  assert.ok(joinedIndex === 1, 'both branch actions ran before the walker could reach fan-in/next');
});

test('with a dispatchFanOut handler supplied, the walker hands off instead of falling back', async () => {
  const doc = fanOutDoc();
  let dispatched = null;
  const result = await walkGraph(doc, {
    dispatchFanOut: async (node) => {
      dispatched = node.id;
      return { summary: 'ok' };
    },
  });
  assert.equal(dispatched, 'split');
  assert.equal(result.outcome, 'success');
  const entry = result.transcript.find((t) => t.event === 'fan-out-dispatched');
  assert.ok(entry);
  assert.deepEqual(entry.branches, ['left', 'right']);
});

// --- escalation nodes (REQ-17) ---

function escalationDoc() {
  return {
    graph_id: 'g',
    schema_version: 1,
    entry: 'ask',
    nodes: [
      {
        id: 'ask',
        kind: 'escalation',
        prompt: 'Pick a lane.',
        branches: [
          { when: 'left', to: 'do-left' },
          { when: 'right', to: 'do-right' },
        ],
      },
      { id: 'do-left', kind: 'action', procedure: 'go left', terminal: true, outcome: 'success' },
      { id: 'do-right', kind: 'action', procedure: 'go right', terminal: true, outcome: 'success' },
    ],
  };
}

test('walks an escalation node by following the human-resolved branch (REQ-17)', async () => {
  const doc = escalationDoc();
  const result = await walkGraph(doc, { resolveEscalation: async () => 'right' });
  assert.equal(result.outcome, 'success');
  const refs = result.transcript.map((t) => t.ref);
  assert.deepEqual(refs, ['g#ask:right', 'g#do-right']);
});

test('resolveEscalation is only ever asked once per visit, never derived from a probe loop', async () => {
  const doc = escalationDoc();
  let calls = 0;
  await walkGraph(doc, {
    resolveEscalation: async () => {
      calls++;
      return 'left';
    },
  });
  assert.equal(calls, 1);
});

test('an escalation answer that is not a declared "when" key is rejected (REQ-17)', async () => {
  const doc = escalationDoc();
  await assert.rejects(
    () => walkGraph(doc, { resolveEscalation: async () => 'middle' }),
    /not one of its declared "when" keys/
  );
});

test('reaching an escalation node with no resolveEscalation handler halts the walk (REQ-17)', async () => {
  const doc = escalationDoc();
  await assert.rejects(() => walkGraph(doc, {}), /halts the walk pending a human choice/);
});
