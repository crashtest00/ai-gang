'use strict';

// Dependency-aware subtask handling.
//
// ScrumMaster is not an AI agent, and from v5.1 it is not a tracker client
// either: this module decides nothing and writes nothing itself. It forwards
// one complete Refinement Agent decomposition to `core` as a single canonical
// command and lets `core`'s own materializer apply it — order-independently,
// atomically on an invalid owner, and idempotently — against the canonical
// store, which is the only place a dependency graph exists.

const canonicalWorkItemsDefault = require('./canonicalWorkItems');

// Forward one complete Refinement Agent decomposition to `core`, in every
// mode.
//
// message: { parentId, subtasks } — `parentId` is the parent work item's
// canonical id; subtasks keep the existing
// { id, displayName, description, agent, "Blocked By" } shape.
// projectName: the normalized project name the command is published for.
// deps: { canonicalWorkItems } — injectable for unit testing.
//
// Publishing a single Streams command to `core`'s command channel is the
// whole of it: `core`'s materialize.py applies the same order-independent,
// atomic-rejection algorithm against its own store. This function does not
// validate agents or write anything, because the write-gating and
// single-validator rules both require that to happen only inside the
// internal API's own write path, not duplicated here. There is no mode branch
// (REQ-06): for a project in Jira mode `core`'s write gate refuses the
// command and it is dead-lettered as WRITE_GATE_REJECTED, and executing a
// decomposition against Jira is v5.2's.
async function routeMaterialization(message, projectName, deps = {}) {
  const canonicalWorkItems = deps.canonicalWorkItems || canonicalWorkItemsDefault;

  return canonicalWorkItems.publishCommand(projectName, {
    command: 'materializeDecomposition',
    actor: 'refinement-agent',
    message: { parentWorkItemId: message.parentId, subtasks: message.subtasks },
  });
}

module.exports = {
  routeMaterialization,
};
