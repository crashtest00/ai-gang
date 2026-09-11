'use strict';

// ScrumMaster's client onto the Internal Work-Item Service
// (the internal-work-item-service design,
// canonical-work-model.md). Two interfaces, matching that document exactly:
//
//  - getMode(): a direct, synchronous HTTP read (REQ-04) — not a Streams
//    round-trip, since it has no state-changing effect.
//  - publishCommand(): every write goes over Redis Streams (REQ-03) using
//    the SAME envelope/streams machinery ScrumMaster already uses for every
//    other flow (envelope.js/streams.js) — this module does not invent a
//    second write mechanism, it targets a different stream.
//
// Reused, not duplicated: stream naming here matches
// services/work-item-service/src/streamTopology.js's commandStreamName() exactly
// (documented in that module as the shared contract both sides must agree
// on). Kept as a small local literal rather than a cross-service require in
// this direction — see services/work-item-service/src/store.js's module comment for
// why the coupling runs the OTHER way (work-item-service requires
// ScrumMaster's pure validation/streams modules) and not this one: a
// deployed ScrumMaster should not need work-item-service's source tree on
// its filesystem, only the stream-naming contract they both already share
// via redis-streams.md's topology convention.

const axios = require('axios');
const redis = require('./redis');
const streams = require('./streams');
const registry = require('./registry');
const { buildEnvelope, KIND } = require('./envelope');

function baseUrl() {
  return process.env.WORKITEM_SERVICE_URL || 'http://work-item-service:9100';
}

function commandStreamName(project) {
  return `aigang:workitems:${registry.normalizeProjectName(project)}`;
}

// REQ-04 direct read. `client` is axios by default, injectable for testing.
async function getMode(project, { client = axios } = {}) {
  const { data } = await client.get(`${baseUrl()}/projects/${encodeURIComponent(project)}/mode`);
  return data; // { project, mode, jiraProjectKey }
}

async function getWorkItem(id, { client = axios, full = false } = {}) {
  try {
    const { data } = await client.get(`${baseUrl()}/work-items/${encodeURIComponent(id)}${full ? '?full=true' : ''}`);
    return data;
  } catch (err) {
    if (err.response && err.response.status === 404) return null;
    throw err;
  }
}

// REQ-03: publish a command onto the internal API's Streams command
// channel. `payload` is the same { command, actor, ... } shape
// services/work-item-service/src/commandConsumer.js expects.
async function publishCommand(project, payload, { redisClient, dedupeKey } = {}) {
  const client = redisClient || redis.getClient();
  const envelope = buildEnvelope({
    kind: KIND.WORK_ITEM_COMMAND,
    project: registry.normalizeProjectName(project),
    payload,
  });
  return streams.publish(client, commandStreamName(project), envelope, { dedupeKey });
}

module.exports = { baseUrl, commandStreamName, getMode, getWorkItem, publishCommand };
