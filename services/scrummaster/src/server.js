'use strict';

const express = require('express');
const redis = require('./redis');
const registry = require('./registry');
const streams = require('./streams');

// canonical-work-model.md REQ-09 (amended 2026-09-09) / internal-work-item-
// service.md REQ-08 (amended 2026-09-09): Django/work-item-service is AI
// Gang's sole external-facing surface. ScrumMaster no longer accepts any
// inbound webhook and exposes no externally reachable route — this HTTP
// server exists only for /health, reachable on the Docker network (see
// docker-compose.yml, which no longer publishes a host port for it), not
// from outside it.
function createServer() {
  const app = express();
  app.use(express.json());

  app.get('/health', async (_req, res) => {
    const client = redis.getClient();
    const reports = [];
    for (const projectName of registry.getProjectNames()) {
      const gateway = await streams.health(client, registry.gatewayStreamName(projectName), registry.GATEWAY_GROUP);
      // REQ-21 — the dispatch-trigger consumer's own health, replacing the
      // former webhook-ingestion-consumer entry now that ScrumMaster has
      // no webhook consumer group at all (REQ-09's amendment: "ScrumMaster
      // MUST NOT retain a consumer group" on the webhook stream).
      const dispatch = await streams.health(
        client, registry.workItemEventStreamName(projectName), registry.DISPATCH_GROUP
      );
      reports.push(
        { ...gateway, status: streams.classifyHealth(gateway) },
        { ...dispatch, status: streams.classifyHealth(dispatch) },
      );
      for (const agent of registry.getEffectiveAgents(projectName)) {
        const suffix = agent.routing.channelSuffix;
        const agentHealth = await streams.health(
          client,
          registry.agentStreamName(projectName, suffix),
          registry.agentGroupName(suffix)
        );
        reports.push({ ...agentHealth, status: streams.classifyHealth(agentHealth) });
      }
    }
    const overallStatus = reports.some(r => r.status === 'unhealthy')
      ? 'unhealthy'
      : reports.some(r => r.status === 'degraded')
        ? 'degraded'
        : 'ok';
    res.status(overallStatus === 'unhealthy' ? 503 : 200).json({ status: overallStatus, streams: reports });
  });

  return app;
}

module.exports = { createServer };
