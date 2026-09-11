'use strict';

const axios = require('axios');

const client = axios.create({
  baseURL: `${process.env.JIRA_BASE_URL}/rest/api/3`,
  auth: {
    username: process.env.JIRA_USER_EMAIL,
    password: process.env.JIRA_API_TOKEN,
  },
  headers: { 'Content-Type': 'application/json' },
});

// Extracts plain text from Atlassian Document Format (ADF)
function adfToText(node) {
  if (!node) return '';
  if (typeof node === 'string') return node;
  if (node.type === 'text') return node.text || '';
  if (node.type === 'hardBreak') return '\n';
  if (node.content && Array.isArray(node.content)) {
    const sep = ['paragraph', 'heading', 'bulletList', 'orderedList', 'listItem'].includes(node.type)
      ? '\n'
      : '';
    return node.content.map(adfToText).join('') + sep;
  }
  return '';
}

// Fetch a full issue with all fields ScrumMaster needs
async function getIssue(key) {
  const agentField    = process.env.JIRA_AGENT_FIELD_ID;
  const blockedField  = process.env.JIRA_BLOCKED_FIELD_ID;
  const vhField       = process.env.JIRA_VALUE_HYPOTHESIS_FIELD_ID;
  const tmField       = process.env.JIRA_TEST_MEASUREMENT_FIELD_ID;
  const behaviorField = process.env.JIRA_BEHAVIOR_FIELD_ID;
  const acField       = process.env.JIRA_AC_FIELD_ID;
  const consField     = process.env.JIRA_CONSTRAINTS_FIELD_ID;
  const edgeField     = process.env.JIRA_EDGE_CASES_FIELD_ID;
  const oosField      = process.env.JIRA_OUT_OF_SCOPE_FIELD_ID;

  // Release ticket fields
  const targetProjectField  = process.env.JIRA_TARGET_PROJECT_FIELD_ID;
  const releaseNotesField   = process.env.JIRA_RELEASE_NOTES_FIELD_ID;
  const candidateShaField   = process.env.JIRA_CANDIDATE_SHA_FIELD_ID;
  const buildIdField        = process.env.JIRA_BUILD_IDENTIFIER_FIELD_ID;
  const previewUrlField     = process.env.JIRA_PREVIEW_URL_FIELD_ID;

  const fields = [
    'summary', 'description', 'status', 'comment', 'resolution',
    'issuetype', 'parent', 'project',
    agentField, blockedField,
    vhField, tmField, behaviorField, acField, consField, edgeField, oosField,
    targetProjectField, releaseNotesField, candidateShaField, buildIdField, previewUrlField,
  ].filter(Boolean).join(',');

  const { data } = await client.get(`/issue/${key}`, { params: { fields } });
  const f = data.fields;

  return {
    key: data.key,
    summary: f.summary,
    description: adfToText(f.description).trim(),
    status: f.status?.name,
    resolution: f.resolution?.name || null,
    issuetype: f.issuetype?.name,
    project: f.project?.key,
    projectName: f.project?.name,
    agent: agentField ? (f[agentField]?.value ?? f[agentField]) : null,
    blocked: blockedField ? (f[blockedField]?.value ?? f[blockedField]) : null,
    parent: f.parent?.key || null,
    comments: (f.comment?.comments || []).map(c => ({
      author: c.author?.displayName || 'Unknown',
      body: adfToText(c.body).trim(),
      timestamp: c.created,
    })),
    // Story schema fields (paragraph text, may be null if not set)
    valueHypothesis:    vhField       ? adfToText(f[vhField]).trim()       || null : null,
    testMeasurement:    tmField       ? adfToText(f[tmField]).trim()        || null : null,
    behavior:           behaviorField ? adfToText(f[behaviorField]).trim()  || null : null,
    acceptanceCriteria: acField       ? adfToText(f[acField]).trim()        || null : null,
    constraints:        consField     ? adfToText(f[consField]).trim()      || null : null,
    edgeCases:          edgeField     ? adfToText(f[edgeField]).trim()      || null : null,
    outOfScope:         oosField      ? adfToText(f[oosField]).trim()       || null : null,
    // Release ticket fields (may be null on non-Release issue types).
    // Target Project is a Jira "project picker" field — value shape is { key, name, ... }.
    // `targetProject` (key, e.g. "HW") is used in JQL; `targetProjectName` (e.g. "hello-world")
    // matches the PROJECT_NAME convention used to route Jenkins jobs and Redis channels.
    targetProject:     targetProjectField ? (f[targetProjectField]?.key ?? f[targetProjectField]) || null : null,
    targetProjectName: targetProjectField ? (f[targetProjectField]?.name ?? null) : null,
    releaseNotes:    releaseNotesField  ? adfToText(f[releaseNotesField]).trim() || null : null,
    candidateSha:    candidateShaField  ? f[candidateShaField] || null : null,
    buildIdentifier: buildIdField       ? f[buildIdField] || null : null,
    previewUrl:      previewUrlField    ? f[previewUrlField] || null : null,
  };
}

// Search for issues matching a JQL query. Returns an array of { key } objects.
// Used for the beta-queue-clean check ahead of cutting a release candidate.
//
// Uses /search/jql (the legacy /search endpoint was removed by Atlassian —
// see https://developer.atlassian.com/changelog/#CHANGE-2046). This is a
// single bounded lookup, not a crawl: it takes only the first page and does
// not follow nextPageToken, matching prior behavior against /search.
async function searchIssues(jql) {
  const { data } = await client.get('/search/jql', { params: { jql, fields: 'key', maxResults: 100 } });
  return (data.issues || []).map(i => ({ key: i.key }));
}

// Post a plain-text comment to an issue
async function postComment(key, text) {
  await client.post(`/issue/${key}/comment`, {
    body: {
      type: 'doc',
      version: 1,
      content: [{
        type: 'paragraph',
        content: [{ type: 'text', text }],
      }],
    },
  });
}

// Update a single custom field on an issue
async function setField(key, fieldId, value) {
  await client.put(`/issue/${key}`, { fields: { [fieldId]: value } });
}

// Set the Agent custom field
async function setAgentField(key, value) {
  const fieldId = process.env.JIRA_AGENT_FIELD_ID;
  if (!fieldId) throw new Error('JIRA_AGENT_FIELD_ID not set');
  await setField(key, fieldId, { value });
}

// Set the Blocked custom field.
// The field is a single-select with one option "Yes".
// Pass true to block, false/null to clear.
async function setBlockedField(key, blocked) {
  const fieldId = process.env.JIRA_BLOCKED_FIELD_ID;
  if (!fieldId) throw new Error('JIRA_BLOCKED_FIELD_ID not set');
  await setField(key, fieldId, blocked ? { value: 'Yes' } : null);
}

// Transition an issue to a named status
async function transitionIssue(key, statusName) {
  const { data } = await client.get(`/issue/${key}/transitions`);
  const transition = data.transitions.find(t => t.to.name === statusName);
  if (!transition) {
    console.warn(`[jira] Transition to "${statusName}" not found for ${key} — skipping`);
    return;
  }
  await client.post(`/issue/${key}/transitions`, { transition: { id: transition.id } });
}

// Create a subtask under a parent issue
// Returns the new subtask key (e.g. "GANG-43")
// Used by the connect-Jira catch-up push: creates a
// top-level issue (not a subtask — see createSubtask for that) for a
// canonical work item with no Jira counterpart yet.
async function createIssue(projectKey, issueTypeName, summary, description) {
  const fields = {
    project: { key: projectKey },
    summary,
    description: {
      type: 'doc',
      version: 1,
      content: [{
        type: 'paragraph',
        content: [{ type: 'text', text: description || summary }],
      }],
    },
    issuetype: { name: issueTypeName },
  };

  const { data } = await client.post('/issue', { fields });
  return data.key;
}

async function createSubtask(parentKey, projectKey, summary, description, agentFieldValue) {
  const fields = {
    project: { key: projectKey },
    parent: { key: parentKey },
    summary,
    description: {
      type: 'doc',
      version: 1,
      content: [{
        type: 'paragraph',
        content: [{ type: 'text', text: description }],
      }],
    },
    issuetype: { name: 'Sub-task' },
  };

  const agentField = process.env.JIRA_AGENT_FIELD_ID;
  if (agentField && agentFieldValue) {
    fields[agentField] = { value: agentFieldValue };
  }

  const { data } = await client.post('/issue', { fields });
  return data.key;
}

// Read-only: fetch the current options on the Agent single-select field's
// first context, e.g. [{ id, value, disabled }, ...]. Used by the periodic
// Jira catalog-drift audit — never used to
// determine whether an agent identity is valid for runtime assignment,
// which is decided solely by the agents.json catalog.
async function getAgentFieldOptions() {
  const fieldId = process.env.JIRA_AGENT_FIELD_ID;
  if (!fieldId) throw new Error('JIRA_AGENT_FIELD_ID not set');

  const { data: contexts } = await client.get(`/field/${fieldId}/context`);
  const contextId = contexts.values?.[0]?.id;
  if (!contextId) return [];

  const { data } = await client.get(`/field/${fieldId}/context/${contextId}/option`);
  return (data.values || []).map(o => ({
    id: o.optionId ?? o.id,
    value: o.value,
    disabled: Boolean(o.disabled),
  }));
}

// --- Dependency-handling support ---

// Label prefix used to persist a Refinement Agent proposal UUID on the Jira
// subtask it materialized to. Chosen over a new custom field
// so this feature needs no Jira-side provisioning step: labels are queryable
// via JQL and require no field/context setup. displayName is persisted as the
// subtask's own summary, so together the label + summary satisfy "persist the
// proposal UUID and displayName on the Jira subtask."
const PROPOSAL_LABEL_PREFIX = 'aigang-proposal-';

function proposalLabel(proposalId) {
  return `${PROPOSAL_LABEL_PREFIX}${proposalId}`;
}

function proposalIdFromLabel(label) {
  return typeof label === 'string' && label.startsWith(PROPOSAL_LABEL_PREFIX)
    ? label.slice(PROPOSAL_LABEL_PREFIX.length)
    : null;
}

// Resolve and cache the Jira issue-link-type id whose outward relationship is
// "blocks" (runtime behavior must key off the id, not a mutable
// display label). JIRA_BLOCKS_LINK_TYPE_ID lets an operator pin the id
// explicitly; otherwise it is discovered once from Jira's built-in "Blocks"
// link type and cached for the life of the process.
let cachedBlocksLinkTypeId = null;

async function getBlocksLinkTypeId() {
  if (cachedBlocksLinkTypeId) return cachedBlocksLinkTypeId;

  const configured = process.env.JIRA_BLOCKS_LINK_TYPE_ID;
  if (configured) {
    cachedBlocksLinkTypeId = configured;
    return cachedBlocksLinkTypeId;
  }

  const { data } = await client.get('/issueLinkType');
  const match = (data.issueLinkTypes || []).find(
    t => t.outward?.toLowerCase() === 'blocks' || t.name?.toLowerCase() === 'blocks'
  );
  if (!match) {
    throw new Error(
      '[jira] No "Blocks" issue link type found on this Jira instance and JIRA_BLOCKS_LINK_TYPE_ID is not set — cannot create or read dependency links'
    );
  }
  cachedBlocksLinkTypeId = match.id;
  return cachedBlocksLinkTypeId;
}

// Fetch every Sub-task under a parent issue, with the label and status fields
// dependency handling needs. Used to recover already-materialized proposals
// on redelivery — no local cache is kept between calls.
//
// Uses /search/jql (the legacy /search endpoint was removed by Atlassian —
// see https://developer.atlassian.com/changelog/#CHANGE-2046). This is a
// single bounded lookup, not a crawl: it takes only the first page and does
// not follow nextPageToken, matching prior behavior against /search.
async function getSubtasksByParent(parentKey) {
  const { data } = await client.get('/search/jql', {
    params: {
      jql: `parent = "${parentKey}"`,
      fields: 'summary,status,labels',
      maxResults: 200,
    },
  });
  return (data.issues || []).map(i => ({
    key: i.key,
    summary: i.fields.summary,
    status: i.fields.status?.name,
    labels: i.fields.labels || [],
  }));
}

// Fetch the configured-link-type relationships for one issue, normalized to
// the dependency direction: `blocks` = issues this one blocks (outward side),
// `isBlockedBy` = issues this one is blocked by (inward side). Unrelated link
// types on the issue are ignored.
async function getIssueLinks(key) {
  const { data } = await client.get(`/issue/${key}`, { params: { fields: 'issuelinks' } });
  const linkTypeId = await getBlocksLinkTypeId();

  const blocks = [];
  const isBlockedBy = [];
  for (const link of data.fields.issuelinks || []) {
    if (link.type?.id !== linkTypeId) continue;
    if (link.outwardIssue) blocks.push(link.outwardIssue.key);
    if (link.inwardIssue) isBlockedBy.push(link.inwardIssue.key);
  }
  return { blocks, isBlockedBy };
}

// Create a `blockerKey blocks dependentKey` / `dependentKey is blocked by
// blockerKey` link using the configured link type. Jira does not dedupe
// identical links on repeated creation — callers must check getIssueLinks
// first to stay idempotent across redelivery.
async function createIssueLink(blockerKey, dependentKey) {
  const linkTypeId = await getBlocksLinkTypeId();
  await client.post('/issueLink', {
    type: { id: linkTypeId },
    outwardIssue: { key: blockerKey },
    inwardIssue: { key: dependentKey },
  });
}

// Create a subtask for one Refinement Agent decomposition proposal, labeled
// with its proposal UUID so a later redelivery can recognize it as already
// materialized. Kept separate from createSubtask (used by
// the unrelated create_subtask gateway operation) rather than changing that
// function's signature.
async function createSubtaskForProposal(parentKey, projectKey, { summary, description, agentFieldValue, proposalId }) {
  const fields = {
    project: { key: projectKey },
    parent: { key: parentKey },
    summary,
    description: {
      type: 'doc',
      version: 1,
      content: [{
        type: 'paragraph',
        content: [{ type: 'text', text: description || '' }],
      }],
    },
    issuetype: { name: 'Sub-task' },
    labels: [proposalLabel(proposalId)],
  };

  const agentField = process.env.JIRA_AGENT_FIELD_ID;
  if (agentField && agentFieldValue) {
    fields[agentField] = { value: agentFieldValue };
  }

  const { data } = await client.post('/issue', { fields });
  return data.key;
}

module.exports = {
  getIssue,
  postComment,
  setAgentField,
  setBlockedField,
  transitionIssue,
  createIssue,
  createSubtask,
  searchIssues,
  getAgentFieldOptions,
  proposalLabel,
  proposalIdFromLabel,
  getSubtasksByParent,
  getIssueLinks,
  createIssueLink,
  createSubtaskForProposal,
};
