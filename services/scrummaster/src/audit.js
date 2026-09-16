'use strict';

// Periodic Jira Agent-field drift audit. Compares the live Jira Agent
// single-select options against the
// agents.json catalog and reports drift; it never modifies agents.json or
// Jira, and drift here MUST NOT cause runtime assignment validation
// (services/scrummaster/src/assignment.js) to accept a Jira-only value — that
// validator never reads Jira at all.

const jira = require('./jira');
const registry = require('./registry');

// Compare the catalog against live Jira Agent-field options.
// Returns { missing, unexpected, mismatched, retiredButEnabled, ok }.
//
//   missing            catalog id has no corresponding Jira option at all —
//                       needs scripts/reconcile-agent-field.sh (creates it).
//   unexpected         a Jira option whose value is not, and never was
//                       (per retiredAgents), part of the catalog — likely a
//                       manual/erroneous addition.
//   mismatched         a currently active catalog id has a Jira option that
//                       is disabled — an active agent that new work can't be
//                       assigned to in Jira's UI.
//   retiredButEnabled  a Jira option whose value matches a formally retired
//                       catalog id (agents.json retiredAgents) but is still
//                       enabled in Jira — retirement wasn't (fully) applied.
function diffAgentFieldOptions(catalogIds, retiredIds, jiraOptions) {
  const catalogSet = new Set(catalogIds);
  const retiredSet = new Set(retiredIds);
  const optionByValue = new Map(jiraOptions.map(o => [o.value, o]));

  const missing = catalogIds.filter(id => !optionByValue.has(id));

  const unexpected = jiraOptions
    .filter(o => !catalogSet.has(o.value) && !retiredSet.has(o.value))
    .map(o => o.value);

  const mismatched = catalogIds
    .filter(id => optionByValue.has(id) && optionByValue.get(id).disabled)
    .map(id => id);

  const retiredButEnabled = jiraOptions
    .filter(o => retiredSet.has(o.value) && !o.disabled)
    .map(o => o.value);

  return {
    missing,
    unexpected,
    mismatched,
    retiredButEnabled,
    ok: missing.length === 0 && unexpected.length === 0 && mismatched.length === 0 && retiredButEnabled.length === 0,
  };
}

// An installation with no Jira connection has nothing to audit: the Agent
// field this compares against only exists inside a Jira instance. Without
// this check the audit calls the shipped template's placeholder host on
// every boot and logs the resulting failure, which reads as a fault when it
// is simply an installation that does not use Jira.
function noJiraToAudit() {
  if (jira.isConfigured()) return false;
  console.log('[audit] No Jira connection is configured — skipping the Agent field drift check.');
  return true;
}

// Run one audit pass. Returns null when there is no Jira to audit. Never
// throws for drift — only for a Jira API failure, which the caller should
// log and treat as "audit did not complete" rather than "no drift found".
async function auditAgentFieldDrift() {
  if (noJiraToAudit()) return null;

  const catalogIds = registry.getAllAgentIds();
  const retiredIds = registry.getRetiredAgentIds();
  const jiraOptions = await jira.getAgentFieldOptions();

  const result = diffAgentFieldOptions(catalogIds, retiredIds, jiraOptions);

  if (result.ok) {
    console.log('[audit] Agent field drift check: no drift.');
    return result;
  }

  console.warn('[audit] Agent field drift detected:');
  if (result.missing.length > 0) {
    console.warn(`  missing (in catalog, no Jira option): ${result.missing.join(', ')}`);
  }
  if (result.unexpected.length > 0) {
    console.warn(`  unexpected (Jira option, not in catalog): ${result.unexpected.join(', ')}`);
  }
  if (result.mismatched.length > 0) {
    console.warn(`  mismatched (active catalog id, Jira option disabled): ${result.mismatched.join(', ')}`);
  }
  if (result.retiredButEnabled.length > 0) {
    console.warn(`  retired-but-enabled (retired catalog id, Jira option still enabled): ${result.retiredButEnabled.join(', ')}`);
  }
  console.warn('  Recovery: run scripts/reconcile-agent-field.sh to sync Jira options from agents.json.');

  return result;
}

// Run the audit at startup and every intervalMs thereafter. Logs and
// swallows Jira API failures so a transient Jira outage doesn't crash
// ScrumMaster or block the next scheduled attempt. Returns null, with no
// timer at all, when there is no Jira to audit.
function scheduleAgentFieldAudit(intervalMs = 24 * 60 * 60 * 1000) {
  if (noJiraToAudit()) return null;

  const run = () => {
    auditAgentFieldDrift().catch(err => {
      console.error('[audit] Agent field drift check failed:', err.message);
    });
  };
  run();
  return setInterval(run, intervalMs);
}

module.exports = { auditAgentFieldDrift, scheduleAgentFieldAudit, diffAgentFieldOptions };
