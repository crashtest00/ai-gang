'use strict';

// Stable node/branch addressing scheme:
//
//   <graph_id>#<node_id>                      — a node
//   <graph_id>#<node_id>:<when_key>            — a decision or escalation
//                                                 node's branch edge
//   <graph_id>#<node_id>:next                  — an action/remediation/fan-in
//                                                 node's sole outgoing edge
//   <graph_id>#<node_id>:<branch_id>           — a fan-out node's branch edge
//
// This module only knows the string shape. Whether a given edge key is
// actually valid for a given node's kind is a schema-level question
// (see schema.js's resolveRef, which uses parseRef then checks the node).

const REF_PATTERN = /^([^#]+)#([^:]+)(?::(.+))?$/;

/**
 * Format a node reference: `<graph_id>#<node_id>`.
 */
function formatNodeRef(graphId, nodeId) {
  if (!graphId || !nodeId) {
    throw new Error('formatNodeRef requires graphId and nodeId');
  }
  return `${graphId}#${nodeId}`;
}

/**
 * Format an edge reference: `<graph_id>#<node_id>:<edgeKey>`.
 * edgeKey is a decision or escalation node's `when` value, the
 * literal string "next" for an action/remediation/fan-in node, or a
 * fan-out node's `branch_id`.
 */
function formatEdgeRef(graphId, nodeId, edgeKey) {
  if (!edgeKey) {
    throw new Error('formatEdgeRef requires an edgeKey');
  }
  return `${formatNodeRef(graphId, nodeId)}:${edgeKey}`;
}

/**
 * Parse a reference of either form into { graphId, nodeId, edgeKey }.
 * edgeKey is null for a bare node reference.
 * Throws on malformed input.
 */
function parseRef(ref) {
  if (typeof ref !== 'string' || ref.length === 0) {
    throw new Error(`parseRef: not a string reference: ${JSON.stringify(ref)}`);
  }
  const match = REF_PATTERN.exec(ref);
  if (!match) {
    throw new Error(`parseRef: malformed reference "${ref}" — expected "<graph_id>#<node_id>[:<key>]"`);
  }
  const [, graphId, nodeId, edgeKey] = match;
  return { graphId, nodeId, edgeKey: edgeKey ?? null };
}

/**
 * Resolve a reference against a loaded, already-validated graph document.
 * Returns { node, edgeKey, target } where target is the resolved node id
 * the edge points to (undefined for a bare node reference).
 * Throws if the reference does not resolve to exactly one node/edge in this
 * document: a reference of any form must resolve to exactly
 * one node or edge in exactly one graph document.
 */
function resolveRef(graphDoc, ref) {
  const { graphId, nodeId, edgeKey } = parseRef(ref);
  if (graphDoc.graph_id !== graphId) {
    throw new Error(`resolveRef: reference "${ref}" names graph "${graphId}", but this document is "${graphDoc.graph_id}"`);
  }
  const node = (graphDoc.nodes || []).find((n) => n.id === nodeId);
  if (!node) {
    throw new Error(`resolveRef: no node "${nodeId}" in graph "${graphId}"`);
  }
  if (edgeKey === null) {
    return { node, edgeKey: null, target: undefined };
  }

  switch (node.kind) {
    case 'decision':
    case 'escalation': {
      const branch = (node.branches || []).find((b) => b.when === edgeKey);
      if (!branch) {
        throw new Error(`resolveRef: ${node.kind} node "${nodeId}" has no branch "when: ${edgeKey}"`);
      }
      return { node, edgeKey, target: branch.to };
    }
    case 'action':
    case 'remediation':
    case 'fan-in': {
      if (edgeKey !== 'next') {
        throw new Error(`resolveRef: ${node.kind} node "${nodeId}" only has a "next" edge, not "${edgeKey}"`);
      }
      if (node.terminal) {
        throw new Error(`resolveRef: node "${nodeId}" is terminal and has no outgoing edge`);
      }
      return { node, edgeKey, target: node.next };
    }
    case 'fan-out': {
      const branch = (node.branches || []).find((b) => b.branch_id === edgeKey);
      if (!branch) {
        throw new Error(`resolveRef: fan-out node "${nodeId}" has no branch_id "${edgeKey}"`);
      }
      return { node, edgeKey, target: branch.to };
    }
    default:
      throw new Error(`resolveRef: unknown node kind "${node.kind}" for node "${nodeId}"`);
  }
}

module.exports = { formatNodeRef, formatEdgeRef, parseRef, resolveRef };
