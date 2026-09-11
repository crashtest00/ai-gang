'use strict';

// canonical-work-model.md REQ-15 — the downstream half of the connect-Jira
// catch-up push. services/work-item-service/workitems/catchup.py selects local work
// items still missing an external_key and emits one
// work_item.jira_catchup_requested event per item via the normal outbox/
// relay mechanism (internal-work-item-service.md REQ-05/REQ-06); catchup.py's
// own module comment names this module as the other half of that split: "a
// downstream Jira-facing consumer (ScrumMaster's jira.js) creates the actual
// Jira issue and reports the resulting key back via recordExternalKey,
// itself idempotent."
//
// One durable consumer per project on the internal work-item service's
// outbound event stream (aigang:workitems:{project}:events, group
// "jira-catchup") — the same "any interested subscriber creates its own
// group" pattern REQ-05/REQ-06 describe, mirroring gateway.js's per-project
// consumer setup.

const jira = require('./jira');
const redis = require('./redis');
const registry = require('./registry');
const streams = require('./streams');
const canonicalWorkItems = require('./canonicalWorkItems');

const EVENT_TYPE = 'work_item.jira_catchup_requested';

const ISSUE_TYPE_BY_CANONICAL_TYPE = {
  story: 'Story',
  task: 'Task',
};

// canonical-work-item-schema.md §3.1: "type ... e.g. story, task, subtask.
// Vocabulary owned by this feature" — not an exhaustive enum, so an
// unrecognized type falls back to a capitalized guess rather than failing.
function issueTypeFor(canonicalType) {
  if (ISSUE_TYPE_BY_CANONICAL_TYPE[canonicalType]) return ISSUE_TYPE_BY_CANONICAL_TYPE[canonicalType];
  return canonicalType ? canonicalType.charAt(0).toUpperCase() + canonicalType.slice(1) : 'Task';
}

const consumers = [];

async function startJiraCatchupConsumers() {
  const client = redis.getClient();
  const consumerName = process.env.SCRUMMASTER_CONSUMER_ID || require('os').hostname();

  for (const projectName of registry.getProjectNames()) {
    const stream = registry.workItemEventStreamName(projectName);
    const consumer = streams.createConsumer(client, {
      stream,
      group: registry.JIRA_CATCHUP_GROUP,
      consumerName,
      handler: envelope => handleWorkItemEventEnvelope(envelope, projectName),
    });
    await consumer.start();
    consumers.push(consumer);
    console.log(`[jira-catchup] Consuming ${stream} as ${registry.JIRA_CATCHUP_GROUP}/${consumerName}`);
  }
}

async function stopJiraCatchupConsumers() {
  await Promise.all(consumers.splice(0).map(c => c.stop()));
}

// The outbound event stream is a fan-out: every event type this service
// emits arrives here, not just catch-up requests, so a non-matching event
// is expected and silently ignored rather than an error.
async function handleWorkItemEventEnvelope(envelope, projectName) {
  const payload = envelope.payload || {};
  if (payload.eventType !== EVENT_TYPE) return;

  const workItemId = payload.data && payload.data.workItemId;
  if (!workItemId) return; // malformed — nothing to act on, not worth a retry.

  await pushWorkItemToJira(workItemId, projectName);
}

async function pushWorkItemToJira(workItemId, projectName) {
  const item = await canonicalWorkItems.getWorkItem(workItemId);
  if (!item) return; // deleted/unknown by the time this ran — nothing to push.
  // REQ-15: "a canonical id that already has a recorded Jira key from a
  // prior attempt MUST be skipped rather than re-created" — checked here,
  // not only in recordExternalKey, so a redelivered event (e.g. this
  // handler crashed after creating the Jira issue but before publishing
  // recordExternalKey) can't create a second Jira issue for the same item.
  if (item.external_key) return;

  const { jiraProjectKey } = await canonicalWorkItems.getMode(projectName);
  if (!jiraProjectKey) {
    // Not necessarily a misconfiguration: catchup.py's start_catchup_push
    // and connect_jira (the mode/key flip) are two separate calls, and
    // nothing guarantees this event is processed after the second one
    // lands. Retry rather than dead-letter immediately; a genuine
    // misconfiguration still surfaces once retries are exhausted.
    throw new Error(`project "${projectName}" has no jiraProjectKey configured yet — retrying`);
  }

  let externalKey;
  if (item.type === 'subtask' && item.parent_id) {
    const parent = await canonicalWorkItems.getWorkItem(item.parent_id);
    if (!parent || !parent.external_key) {
      // The parent hasn't been pushed yet. Work items are queued for
      // catch-up in creation order (catchup.py's start_catchup_push), so a
      // parent's event is normally handled first — this is the case where
      // its own recordExternalKey round-trip simply hasn't landed yet.
      // Retry rather than dead-letter: it resolves once that completes.
      throw new Error(`work item ${workItemId}'s parent ${item.parent_id} has no Jira key yet — retrying`);
    }
    externalKey = await jira.createSubtask(
      parent.external_key, jiraProjectKey, item.display_name, item.description || item.display_name
    );
  } else {
    externalKey = await jira.createIssue(jiraProjectKey, issueTypeFor(item.type), item.display_name, item.description);
  }

  await canonicalWorkItems.publishCommand(projectName, {
    command: 'recordExternalKey',
    actor: 'jira-catchup',
    workItemId,
    externalKey,
  });
}

module.exports = {
  startJiraCatchupConsumers,
  stopJiraCatchupConsumers,
  handleWorkItemEventEnvelope,
  pushWorkItemToJira,
  issueTypeFor,
};
