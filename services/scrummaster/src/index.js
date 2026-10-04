'use strict';

require('dotenv').config();

const { connect, getClient } = require('./redis');
const streams = require('./streams');
const registry = require('./registry');
const canonicalWorkItems = require('./canonicalWorkItems');
const { startGatewaySubscriber, stopGatewaySubscriber } = require('./gateway');
const { createServer } = require('./server');
const { startDispatchConsumers, stopDispatchConsumers } = require('./dispatchConsumer');

const PORT = process.env.PORT || 9000;

// core's own consumer group on its command channel
// (aigang:workitems:{project}), matching
// services/core/workitems/stream_topology.py's COMMAND_GROUP exactly — a
// local literal rather than a cross-service require, for the same reason
// canonicalWorkItems.js keeps its own copy of the stream-naming contract
// instead of requiring core's source tree.
const CORE_COMMAND_GROUP = 'core';

// Idempotently create every stream + consumer group this deployment will
// ever address, before any producer or consumer starts (streams/groups
// must exist before producers are enabled, and creating a *new* group on
// restart would incorrectly replay retained history — ensureGroup only
// creates a group the first time it sees one).
async function bootstrapStreams() {
  const client = getClient();

  for (const projectName of registry.getProjectNames()) {
    await streams.ensureGroup(client, registry.gatewayStreamName(projectName), registry.GATEWAY_GROUP);
    await streams.ensureGroup(client, registry.workItemEventStreamName(projectName), registry.DISPATCH_GROUP);

    for (const agent of registry.getEffectiveAgents(projectName)) {
      const suffix = agent.routing.channelSuffix;
      await streams.ensureGroup(
        client,
        registry.agentStreamName(projectName, suffix),
        registry.agentGroupName(suffix)
      );
    }
  }

  console.log('[scrummaster] Stream topology bootstrapped for', registry.getProjectNames().join(', '));
}

// Every stream + group this deployment addresses, for retention trimming.
// Includes core's command stream (BF-08 finding 2): core's own
// trim_acknowledged/trim_dead_letters have no caller, so without this entry
// aigang:workitems:{project} is never trimmed. Same defaults as every other
// entry here — no separate schedule.
function everyStreamGroup() {
  const targets = [];
  for (const projectName of registry.getProjectNames()) {
    targets.push({ stream: registry.gatewayStreamName(projectName), group: registry.GATEWAY_GROUP });
    targets.push({ stream: registry.workItemEventStreamName(projectName), group: registry.DISPATCH_GROUP });
    targets.push({ stream: canonicalWorkItems.commandStreamName(projectName), group: CORE_COMMAND_GROUP });
    for (const agent of registry.getEffectiveAgents(projectName)) {
      const suffix = agent.routing.channelSuffix;
      targets.push({ stream: registry.agentStreamName(projectName, suffix), group: registry.agentGroupName(suffix) });
    }
  }
  return targets;
}

// Bounded retention without premature deletion: acknowledged
// history is trimmed after STREAM_RETENTION_DAYS (default 7), dead-letter
// entries after DEAD_LETTER_RETENTION_DAYS (default 30). Runs at startup and
// every 6 hours thereafter.
function scheduleRetention() {
  const client = getClient();
  const retentionMs = (parseInt(process.env.STREAM_RETENTION_DAYS, 10) || 7) * 24 * 60 * 60 * 1000;
  const deadLetterRetentionMs = (parseInt(process.env.DEAD_LETTER_RETENTION_DAYS, 10) || 30) * 24 * 60 * 60 * 1000;

  const run = async () => {
    for (const { stream, group } of everyStreamGroup()) {
      try {
        await streams.trimAcknowledged(client, stream, group, retentionMs);
        await streams.trimDeadLetters(client, stream, deadLetterRetentionMs);
      } catch (err) {
        console.error(`[scrummaster] Retention trim failed for ${stream}/${group}:`, err.message);
      }
    }
  };
  run();
  return setInterval(run, 6 * 60 * 60 * 1000);
}

async function main() {
  console.log('[scrummaster] Starting...');

  // Fail fast on a malformed agent catalog or project configuration
  // rather than accepting webhook traffic against invalid assignment data.
  registry.load();

  await connect();
  await bootstrapStreams();
  await startGatewaySubscriber();
  await startDispatchConsumers();
  scheduleRetention();

  const app = createServer();
  app.listen(PORT, () => {
    console.log(`[scrummaster] Listening on port ${PORT}`);
  });
}

// BF-08 finding 1: without this, the gateway and dispatch consumer groups
// are never released on shutdown — the mechanism behind a consumer that
// outlived its run holding a group for 3h37m and corrupting later suites
// (audit row 5). Wired to the existing stop functions (gateway.js's
// stopGatewaySubscriber, dispatchConsumer.js's stopDispatchConsumers) —
// no new shutdown mechanism. Guarded against a second signal arriving
// while the first is still shutting down.
let shuttingDown = false;

async function shutdown(signal) {
  if (shuttingDown) return;
  shuttingDown = true;
  console.log(`[scrummaster] ${signal} received, stopping gateway and dispatch consumers...`);
  try {
    await stopGatewaySubscriber();
    await stopDispatchConsumers();
    console.log('[scrummaster] Shutdown complete.');
    process.exit(0);
  } catch (err) {
    console.error('[scrummaster] Error during shutdown:', err);
    process.exit(1);
  }
}

process.on('SIGTERM', () => shutdown('SIGTERM'));
process.on('SIGINT', () => shutdown('SIGINT'));

main().catch(err => {
  console.error('[scrummaster] Fatal error:', err);
  process.exit(1);
});
