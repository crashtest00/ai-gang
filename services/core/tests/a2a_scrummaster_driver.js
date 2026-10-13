'use strict';

// ScrumMaster, run for real, for the durable A2A record's end-to-end tests
// (tests/a2a_support.py's `ScrumMaster`). Not a reimplementation: it loads
// ScrumMaster's own modules from services/scrummaster/src and drives them
// against the test Redis, so every dispatch is `handlers.dispatchTask`'s and
// every verdict is the gateway's, published onto the real command stream for
// core's real command consumer to apply.
//
// Protocol: one JSON request per line on stdin, one JSON reply per line on
// stdout, `{ ok: true, ...result }` or `{ ok: false, error }`. Everything
// ScrumMaster logs goes to stderr. A restart is a new process: taskStore is
// in memory, so a restart empties it, as REQ-04's Acceptance needs.
//
// Requests:
//   dispatch      { workItemId, project, agentId, dispatchId? }
//                 -> { taskId, contextId, messageId }
//   startGateway  { project, retryDelayMs?, reclaimIntervalMs?, maxAttempts?, blockMs? }
//                 the gateway consumer, through gateway._gatewayConsumerOptions
//                 (the handler and dead-letter hook startGatewaySubscriber
//                 wires), with test timing
//   stopGateway   {}
//   failPublishes { count, which: 'projection' | 'verdict' | 'all' }
//                 the next `count` matching publishCommand calls throw
//   taskState     { taskId } -> { state, failedMessageIds, messageIds }
//   ping          {}

const path = require('node:path');
const readline = require('node:readline');

const SRC = path.join(__dirname, '..', '..', 'scrummaster', 'src');
const redisModule = require(path.join(SRC, 'redis'));
const registry = require(path.join(SRC, 'registry'));
const streams = require(path.join(SRC, 'streams'));
const canonicalWorkItems = require(path.join(SRC, 'canonicalWorkItems'));
const handlers = require(path.join(SRC, 'handlers'));
const gateway = require(path.join(SRC, 'gateway'));
const taskStore = require(path.join(SRC, 'a2a', 'taskStore'));

// Everything ScrumMaster prints is diagnostic; stdout carries replies only.
console.log = (...args) => console.error(...args);

const reply = message => process.stdout.write(`${JSON.stringify(message)}\n`);

let gatewayConsumer = null;
const failures = { count: 0, which: 'projection' };
const realPublishCommand = canonicalWorkItems.publishCommand;
canonicalWorkItems.publishCommand = async (project, payload, options) => {
  const isVerdict = payload && payload.command === 'recordA2AVerdict';
  const matches = failures.which === 'all' || (failures.which === 'verdict') === isVerdict;
  if (failures.count > 0 && matches) {
    failures.count -= 1;
    throw new Error(`injected publish failure (${payload.command})`);
  }
  return realPublishCommand(project, payload, options);
};

const operations = {
  async ping() {
    return {};
  },

  async dispatch({ workItemId, project, agentId, dispatchId }) {
    const agent = registry.getAgent(agentId);
    if (!agent) throw new Error(`unknown agent ${agentId}`);
    const issue = { key: workItemId, projectName: project, jiraProjectKey: null };
    await handlers.dispatchTask(issue, agent, {
      dispatchId,
      promptFactory: task => `Work item ${task.id}: do the work.`,
    });
    const record = taskStore.getTaskById(workItemId);
    return { taskId: record.id, contextId: record.contextId, messageId: taskStore.lastMessage(workItemId).messageId };
  },

  async startGateway({ project, retryDelayMs = 200, reclaimIntervalMs = 100, maxAttempts = 3, blockMs = 100 }) {
    if (gatewayConsumer) throw new Error('the gateway consumer is already running');
    gatewayConsumer = streams.createConsumer(redisModule.getClient(), {
      stream: registry.gatewayStreamName(project),
      group: registry.GATEWAY_GROUP,
      consumerName: 'a2a-driver',
      ...gateway._gatewayConsumerOptions(project),
      retryDelayMs,
      reclaimIntervalMs,
      maxAttempts,
      blockMs,
    });
    await gatewayConsumer.start();
    return {};
  },

  async stopGateway() {
    if (gatewayConsumer) await gatewayConsumer.stop();
    gatewayConsumer = null;
    return {};
  },

  async failPublishes({ count, which = 'projection' }) {
    failures.count = count;
    failures.which = which;
    return {};
  },

  async taskState({ taskId }) {
    const record = taskStore.getTaskById(taskId);
    return {
      state: record ? record.state : null,
      failedMessageIds: taskStore.failedMessageIds(taskId),
      messageIds: record ? record.messages.map(m => m.messageId) : [],
    };
  },
};

async function main() {
  await redisModule.connect();
  registry.load();
  reply({ ok: true, ready: true });

  const lines = readline.createInterface({ input: process.stdin });
  for await (const line of lines) {
    if (!line.trim()) continue;
    let request;
    try {
      request = JSON.parse(line);
      const operation = operations[request.op];
      if (!operation) throw new Error(`unknown op ${request.op}`);
      reply({ ok: true, ...(await operation(request)) });
    } catch (err) {
      reply({ ok: false, error: err && err.stack ? err.stack : String(err) });
    }
  }
  if (gatewayConsumer) await gatewayConsumer.stop();
  await redisModule.getClient().quit().catch(() => {});
}

main().catch(err => {
  console.error(err);
  process.exit(1);
});
