'use strict';

const fs = require('fs');
const yaml = require('js-yaml');

const VALID_KINDS = new Set(['decision', 'action', 'remediation', 'fan-out', 'fan-in', 'escalation']);
const VALID_TERMINAL_OUTCOMES = new Set(['success', 'skipped']);

/**
 * Load a `<graph_id>.graph.yaml` file and parse it. Does not validate —
 * call validateGraphDocument on the result.
 */
function loadGraphFile(filePath) {
  const raw = fs.readFileSync(filePath, 'utf8');
  return yaml.load(raw);
}

/**
 * Validate a graph document's structural well-formedness — required
 * top-level fields, each node kind's required shape, dead-end/reachability
 * checks, and fan-out/fan-in well-formedness. Returns
 * { valid: boolean, errors: string[] }.
 *
 * This is deliberately a pure function over a plain object (not tied to
 * file I/O) so it's easy to unit test with inline fixtures — this feature
 * does not mandate a specific validator implementation, as long as the
 * core structural checks continue to hold.
 */
function validateGraphDocument(doc) {
  const errors = [];

  if (!doc || typeof doc !== 'object') {
    return { valid: false, errors: ['document is not an object'] };
  }

  // --- graph_id, schema_version, entry, nodes ---
  if (!doc.graph_id || typeof doc.graph_id !== 'string') {
    errors.push('missing or invalid graph_id');
  }
  if (doc.schema_version === undefined || doc.schema_version === null) {
    errors.push('missing schema_version');
  }
  if (!doc.entry || typeof doc.entry !== 'string') {
    errors.push('missing or invalid entry');
  }
  if (!Array.isArray(doc.nodes)) {
    errors.push('missing or invalid nodes (must be an array)');
    return { valid: false, errors };
  }

  const nodesById = new Map();
  for (const node of doc.nodes) {
    if (!node || typeof node !== 'object' || !node.id) {
      errors.push(`node missing an "id": ${JSON.stringify(node)}`);
      continue;
    }
    if (nodesById.has(node.id)) {
      errors.push(`duplicate node id "${node.id}"`);
      continue;
    }
    nodesById.set(node.id, node);
  }

  if (doc.entry && !nodesById.has(doc.entry)) {
    errors.push(`entry "${doc.entry}" does not name a node in this document`);
  }

  for (const node of nodesById.values()) {
    if (!VALID_KINDS.has(node.kind)) {
      errors.push(`node "${node.id}" has invalid or missing kind "${node.kind}"`);
      continue;
    }
    validateNodeShape(node, nodesById, errors);
  }

  // no unresolved dead ends anywhere reachable from entry
  if (doc.entry && nodesById.has(doc.entry)) {
    validateNoDeadEnds(doc, nodesById, errors);
  }

  // fan-out/fan-in well-formedness
  for (const node of nodesById.values()) {
    if (node.kind === 'fan-out') {
      validateFanOut(node, nodesById, errors);
    }
  }

  return { valid: errors.length === 0, errors };
}

function validateNodeShape(node, nodesById, errors) {
  switch (node.kind) {
    case 'decision':
      validateDecisionNode(node, nodesById, errors);
      break;
    case 'action':
      validateActionNode(node, nodesById, errors);
      break;
    case 'remediation':
      validateRemediationNode(node, nodesById, errors);
      break;
    case 'fan-out':
      validateFanOutShape(node, nodesById, errors);
      break;
    case 'fan-in':
      validateFanInShape(node, nodesById, errors);
      break;
    case 'escalation':
      validateEscalationNode(node, nodesById, errors);
      break;
    default:
      // unreachable — kind already checked by caller
      break;
  }

  if (node.terminal) {
    if (!VALID_TERMINAL_OUTCOMES.has(node.outcome)) {
      errors.push(`node "${node.id}" is terminal but outcome is not "success" or "skipped" (got "${node.outcome}")`);
    }
    if (node.kind === 'remediation') {
      errors.push(`remediation node "${node.id}" MUST NOT be terminal`);
    }
    if (node.kind === 'fan-out') {
      errors.push(`fan-out node "${node.id}" MUST NOT be terminal`);
    }
  }
}

function validateDecisionNode(node, nodesById, errors) {
  if (node.writes) {
    errors.push(`decision node "${node.id}" MUST NOT declare writes (decision nodes MUST NOT mutate state)`);
  }
  if (!node.check || typeof node.check !== 'object') {
    errors.push(`decision node "${node.id}" missing "check"`);
  } else if (!node.check.description || !node.check.probe) {
    errors.push(`decision node "${node.id}" check must have "description" and "probe"`);
  }
  if (!Array.isArray(node.branches) || node.branches.length === 0) {
    errors.push(`decision node "${node.id}" missing non-empty "branches"`);
    return;
  }
  const seenWhen = new Set();
  for (const branch of node.branches) {
    if (!branch || typeof branch.when !== 'string' || !branch.to) {
      errors.push(`decision node "${node.id}" has a malformed branch: ${JSON.stringify(branch)}`);
      continue;
    }
    if (seenWhen.has(branch.when)) {
      errors.push(`decision node "${node.id}" has a duplicate "when: ${branch.when}" (branches must be mutually exclusive)`);
    }
    seenWhen.add(branch.when);
    if (!nodesById.has(branch.to)) {
      errors.push(`decision node "${node.id}" branch "when: ${branch.when}" targets unknown node "${branch.to}"`);
    }
  }
  // a distinct outcome for the probe itself failing to execute
  const errorWhen = node.check && node.check.error_when;
  if (!errorWhen) {
    errors.push(`decision node "${node.id}" check must declare "error_when" naming the branch reached when the probe itself fails to execute`);
  } else if (!seenWhen.has(errorWhen)) {
    errors.push(`decision node "${node.id}" check.error_when "${errorWhen}" does not match any declared branch "when" key`);
  }
}

function validateEscalationNode(node, nodesById, errors) {
  if (node.check) {
    errors.push(`escalation node "${node.id}" MUST NOT declare "check" (its outcome is human-resolved, never probe-derived)`);
  }
  if (node.procedure) {
    errors.push(`escalation node "${node.id}" MUST NOT declare "procedure"`);
  }
  if (node.writes) {
    errors.push(`escalation node "${node.id}" MUST NOT declare "writes" (persist a chosen branch's state in a following action node instead)`);
  }
  if (!node.prompt || typeof node.prompt !== 'string') {
    errors.push(`escalation node "${node.id}" missing "prompt"`);
  }
  if (!Array.isArray(node.branches) || node.branches.length === 0) {
    errors.push(`escalation node "${node.id}" missing non-empty "branches"`);
    return;
  }
  const seenWhen = new Set();
  for (const branch of node.branches) {
    if (!branch || typeof branch.when !== 'string' || !branch.to) {
      errors.push(`escalation node "${node.id}" has a malformed branch: ${JSON.stringify(branch)}`);
      continue;
    }
    if (seenWhen.has(branch.when)) {
      errors.push(`escalation node "${node.id}" has a duplicate "when: ${branch.when}" (branches must be mutually exclusive)`);
    }
    seenWhen.add(branch.when);
    if (!nodesById.has(branch.to)) {
      errors.push(`escalation node "${node.id}" branch "when: ${branch.when}" targets unknown node "${branch.to}"`);
    }
  }
}

function validateActionNode(node, nodesById, errors) {
  if (!node.procedure || typeof node.procedure !== 'string') {
    errors.push(`action node "${node.id}" missing "procedure"`);
  }
  validateSingleNextEdge(node, nodesById, errors);
  if (node.writes !== undefined) {
    if (typeof node.writes !== 'object' || node.writes === null) {
      errors.push(`action node "${node.id}" writes must be an object`);
    } else {
      if (!Array.isArray(node.writes.files)) {
        errors.push(`action node "${node.id}" writes.files must be present (an array, possibly empty)`);
      }
      if (!Array.isArray(node.writes.services)) {
        errors.push(`action node "${node.id}" writes.services must be present (an array, possibly empty)`);
      }
    }
  }
}

function validateRemediationNode(node, nodesById, errors) {
  if (!node.guidance || typeof node.guidance !== 'string') {
    errors.push(`remediation node "${node.id}" missing "guidance"`);
  }
  if (node.outcome === 'failure') {
    errors.push(`remediation node "${node.id}" MUST NOT have outcome: failure`);
  }
  validateSingleNextEdge(node, nodesById, errors);
}

function validateSingleNextEdge(node, nodesById, errors) {
  if (node.terminal) {
    return; // terminal nodes have no outgoing edge; checked separately
  }
  if (!node.next || typeof node.next !== 'string') {
    errors.push(`${node.kind} node "${node.id}" missing "next" (required unless terminal)`);
    return;
  }
  if (!nodesById.has(node.next)) {
    errors.push(`${node.kind} node "${node.id}" next "${node.next}" targets unknown node`);
  }
}

function validateFanOutShape(node, nodesById, errors) {
  if (node.check) {
    errors.push(`fan-out node "${node.id}" MUST NOT declare "check"`);
  }
  if (!Array.isArray(node.branches) || node.branches.length === 0) {
    errors.push(`fan-out node "${node.id}" missing non-empty "branches"`);
  } else {
    const seenIds = new Set();
    for (const branch of node.branches) {
      if (!branch || !branch.branch_id || !branch.to) {
        errors.push(`fan-out node "${node.id}" has a malformed branch: ${JSON.stringify(branch)}`);
        continue;
      }
      if (seenIds.has(branch.branch_id)) {
        errors.push(`fan-out node "${node.id}" has a duplicate branch_id "${branch.branch_id}"`);
      }
      seenIds.add(branch.branch_id);
      if (!nodesById.has(branch.to)) {
        errors.push(`fan-out node "${node.id}" branch "${branch.branch_id}" targets unknown node "${branch.to}"`);
      }
    }
  }
  if (!node.join || typeof node.join !== 'string') {
    errors.push(`fan-out node "${node.id}" missing "join"`);
  } else if (!nodesById.has(node.join)) {
    errors.push(`fan-out node "${node.id}" join "${node.join}" targets unknown node`);
  } else if (nodesById.get(node.join).kind !== 'fan-in') {
    errors.push(`fan-out node "${node.id}" join "${node.join}" must name a fan-in node`);
  }
}

function validateFanInShape(node, nodesById, errors) {
  if (!node.for || typeof node.for !== 'string') {
    errors.push(`fan-in node "${node.id}" missing "for"`);
  } else if (!nodesById.has(node.for)) {
    errors.push(`fan-in node "${node.id}" for "${node.for}" targets unknown node`);
  } else {
    const fanOut = nodesById.get(node.for);
    if (fanOut.kind !== 'fan-out') {
      errors.push(`fan-in node "${node.id}" for "${node.for}" must name a fan-out node`);
    } else if (fanOut.join !== node.id) {
      errors.push(`fan-in node "${node.id}" for "${node.for}" does not match back — that fan-out's join is "${fanOut.join}"`);
    }
  }
  validateSingleNextEdge(node, nodesById, errors);
}

function validateNoDeadEnds(doc, nodesById, errors) {
  const reachable = new Set();
  const stack = [doc.entry];
  while (stack.length > 0) {
    const id = stack.pop();
    if (reachable.has(id)) continue;
    reachable.add(id);
    const node = nodesById.get(id);
    if (!node) continue; // dangling ref already reported elsewhere
    for (const targetId of outgoingTargets(node)) {
      if (targetId && !reachable.has(targetId)) stack.push(targetId);
    }
  }

  for (const id of reachable) {
    const node = nodesById.get(id);
    if (!node) continue;
    if (node.terminal) continue;
    const targets = outgoingTargets(node);
    if (targets.length === 0) {
      errors.push(`node "${node.id}" is reachable, non-terminal, and has zero outgoing edges (dead end)`);
    }
  }
}

function outgoingTargets(node) {
  switch (node.kind) {
    case 'decision':
    case 'escalation':
      return (node.branches || []).map((b) => b.to).filter(Boolean);
    case 'fan-out':
      return (node.branches || []).map((b) => b.to).filter(Boolean);
    case 'action':
    case 'remediation':
    case 'fan-in':
      return node.next ? [node.next] : [];
    default:
      return [];
  }
}

// Every path from each of a fan-out node's branches
// reaches the paired fan-in node before reaching any terminal node.
function validateFanOut(fanOutNode, nodesById, errors) {
  if (!fanOutNode.join || !nodesById.has(fanOutNode.join)) {
    return; // already reported by validateFanOutShape
  }
  for (const branch of fanOutNode.branches || []) {
    if (!branch.to || !nodesById.has(branch.to)) continue;
    const visited = new Set();
    const violation = dfsFindsTerminalBeforeJoin(branch.to, fanOutNode.join, nodesById, visited);
    if (violation) {
      errors.push(
        `fan-out node "${fanOutNode.id}" branch "${branch.branch_id}" can reach terminal node "${violation}" without first passing through join "${fanOutNode.join}"`
      );
    }
  }
}

function dfsFindsTerminalBeforeJoin(nodeId, joinId, nodesById, visited) {
  if (nodeId === joinId) return null; // reached the join — this path is fine
  if (visited.has(nodeId)) return null; // cycle (e.g. a remediation loop) — not a violation
  visited.add(nodeId);
  const node = nodesById.get(nodeId);
  if (!node) return null; // dangling ref reported elsewhere
  if (node.terminal) return nodeId; // violation: terminal reached before join
  for (const targetId of outgoingTargets(node)) {
    const result = dfsFindsTerminalBeforeJoin(targetId, joinId, nodesById, visited);
    if (result) return result;
  }
  return null;
}

module.exports = { loadGraphFile, validateGraphDocument };
