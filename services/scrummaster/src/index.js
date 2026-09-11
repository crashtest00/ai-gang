'use strict';

require('dotenv').config();

const { connect, getClient } = require('./redis');
const streams = require('./streams');
const registry = require('./registry');
const { startGatewaySubscriber } = require('./gateway');
const { createServer } = require('./server');
const { scheduleAgentFieldAudit } = require('./audit');
const { startJiraCatchupConsumers } = require('./jiraCatchupConsumer');
const { startDispatchConsumers } = require('./dispatchConsumer');

const PORT = process.env.PORT || 9000;

// Idempotently create every stream + consumer group this deployment will
// ever address, before any producer or consumer starts (redis-streams.md
// REQ-11: streams/groups must exist before producers are enabled, and
// creating a *new* group on restart would incorrectly replay retained
// history — ensureGroup only creates a group the first time it sees one).
async function bootstrapStreams() {
  const client = getClient();

  for (const projectName of registry.getProjectNames()) {
    await streams.ensureGroup(client, registry.gatewayStreamName(projectName), registry.GATEWAY_GROUP);
    await streams.ensureGroup(client, registry.workItemEventStreamName(projectName), registry.JIRA_CATCHUP_GROUP);
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
function everyStreamGroup() {
  const targets = [];
  for (const projectName of registry.getProjectNames()) {
    targets.push({ stream: registry.gatewayStreamName(projectName), group: registry.GATEWAY_GROUP });
    targets.push({ stream: registry.workItemEventStreamName(projectName), group: registry.JIRA_CATCHUP_GROUP });
    targets.push({ stream: registry.workItemEventStreamName(projectName), group: registry.DISPATCH_GROUP });
    for (const agent of registry.getEffectiveAgents(projectName)) {
      const suffix = agent.routing.channelSuffix;
      targets.push({ stream: registry.agentStreamName(projectName, suffix), group: registry.agentGroupName(suffix) });
    }
  }
  return targets;
}

// Bounded retention without premature deletion (REQ-10): acknowledged
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
  // (agent-assignment.md REQ-01, REQ-02) rather than accepting webhook
  // traffic against invalid assignment data.
  registry.load();

  await connect();
  await bootstrapStreams();
  await startGatewaySubscriber();
  await startJiraCatchupConsumers();
  await startDispatchConsumers();
  scheduleRetention();

  // Startup + every-24h Jira Agent-field drift audit (agent-assignment.md
  // REQ-07). Runs against live Jira, so it starts only after redis.connect()
  // succeeds — no point auditing before the service is otherwise healthy.
  scheduleAgentFieldAudit();

  const app = createServer();
  app.listen(PORT, () => {
    console.log(`[scrummaster] Listening on port ${PORT}`);
  });
}

main().catch(err => {
  console.error('[scrummaster] Fatal error:', err);
  process.exit(1);
});
