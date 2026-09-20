'use strict';

const { formatNodeRef, formatEdgeRef } = require('./addressing');

/**
 * Single-node-at-a-time graph walker.
 *
 * `handlers`:
 *   - evaluateCheck(node)      -> Promise<outcomeKey>  (decision nodes)
 *   - runAction(node)          -> Promise<void>         (action nodes, optional)
 *   - onRemediation(node)      -> Promise<void>         (remediation nodes, optional)
 *   - dispatchFanOut(node, ctx)-> Promise<{ summary }>   (fan-out nodes, optional —
 *       when omitted, the walker uses the fail-closed sequential fallback:
 *       one branch at a time, in listing order, never
 *       advancing to fan-in before every branch has completed)
 *   - resolveEscalation(node)  -> Promise<outcomeKey>  (escalation nodes —
 *       the human's chosen `when` key; unlike evaluateCheck this is
 *       never derived from a probe. Omitting this handler means the walker
 *       cannot proceed past an escalation node at all — reaching one always
 *       halts the walk pending a human's choice, and a caller with no way
 *       to ask a human simply cannot resolve one.)
 *
 * Returns { transcript, outcome, node }. `transcript` is an ordered list of
 * fully-qualified references visited: it lists the fully-qualified node
 * reference for every node visited, in the order visited, with no more than
 * one node reference recorded as 'current' at any point in time for a given
 * walker instance.
 *
 * A `fan-out` node's sequential fallback recursively calls walkGraph once
 * per branch — each such call is its own independent walker instance and
 * produces its own transcript, so each spawned instance individually
 * continues to satisfy that same single-node-at-a-time guarantee.
 */
async function walkGraph(graphDoc, handlers = {}, opts = {}) {
  const nodesById = new Map(graphDoc.nodes.map((n) => [n.id, n]));
  const transcript = [];
  let current = opts.entryOverride || graphDoc.entry;
  const maxSteps = opts.maxSteps ?? 1000;
  let steps = 0;

  while (true) {
    if (++steps > maxSteps) {
      throw new Error(`walkGraph: exceeded maxSteps (${maxSteps}) — possible unresolved loop at "${current}"`);
    }
    const node = nodesById.get(current);
    if (!node) {
      throw new Error(`walkGraph: node not found: "${current}"`);
    }
    const nodeRef = formatNodeRef(graphDoc.graph_id, node.id);

    if (node.terminal) {
      transcript.push({ ref: nodeRef, kind: node.kind, event: 'terminal', outcome: node.outcome });
      return { transcript, outcome: node.outcome, node };
    }

    switch (node.kind) {
      case 'decision': {
        const outcomeKey = await handlers.evaluateCheck(node);
        const branch = (node.branches || []).find((b) => b.when === outcomeKey);
        if (!branch) {
          throw new Error(`walkGraph: decision "${node.id}" produced outcome "${outcomeKey}" with no matching branch`);
        }
        transcript.push({
          ref: formatEdgeRef(graphDoc.graph_id, node.id, outcomeKey),
          kind: node.kind,
          event: 'decision',
          outcome: outcomeKey,
          to: branch.to,
        });
        current = branch.to;
        break;
      }
      case 'action': {
        if (handlers.runAction) await handlers.runAction(node);
        transcript.push({
          ref: formatEdgeRef(graphDoc.graph_id, node.id, 'next'),
          kind: node.kind,
          event: 'action',
          to: node.next,
        });
        current = node.next;
        break;
      }
      case 'remediation': {
        if (handlers.onRemediation) await handlers.onRemediation(node);
        transcript.push({
          ref: formatEdgeRef(graphDoc.graph_id, node.id, 'next'),
          kind: node.kind,
          event: 'remediation',
          to: node.next,
        });
        current = node.next;
        break;
      }
      case 'fan-out': {
        if (handlers.dispatchFanOut) {
          const result = await handlers.dispatchFanOut(node, { nodesById, graphDoc, handlers, walkGraph });
          transcript.push({
            ref: nodeRef,
            kind: node.kind,
            event: 'fan-out-dispatched',
            branches: (node.branches || []).map((b) => b.branch_id),
            result: result && result.summary,
          });
        } else {
          // Fail-closed fallback: sequential, one at a time, in
          // listing order — never partially, never advancing to fan-in
          // before every branch has completed.
          const branchResults = [];
          for (const branch of node.branches || []) {
            const sub = await walkGraph(graphDoc, handlers, { ...opts, entryOverride: branch.to });
            branchResults.push({ branch_id: branch.branch_id, outcome: sub.outcome, transcript: sub.transcript });
          }
          transcript.push({
            ref: nodeRef,
            kind: node.kind,
            event: 'fan-out-sequential-fallback',
            branches: branchResults.map((b) => ({ branch_id: b.branch_id, outcome: b.outcome })),
            branchTranscripts: branchResults,
          });
        }
        current = node.join;
        break;
      }
      case 'escalation': {
        if (!handlers.resolveEscalation) {
          throw new Error(
            `walkGraph: escalation "${node.id}" halts the walk pending a human choice, but no resolveEscalation handler was provided`
          );
        }
        const outcomeKey = await handlers.resolveEscalation(node);
        const branch = (node.branches || []).find((b) => b.when === outcomeKey);
        if (!branch) {
          throw new Error(
            `walkGraph: escalation "${node.id}" resolved to "${outcomeKey}", which is not one of its declared "when" keys`
          );
        }
        transcript.push({
          ref: formatEdgeRef(graphDoc.graph_id, node.id, outcomeKey),
          kind: node.kind,
          event: 'escalation',
          outcome: outcomeKey,
          to: branch.to,
        });
        current = branch.to;
        break;
      }
      case 'fan-in': {
        transcript.push({
          ref: formatEdgeRef(graphDoc.graph_id, node.id, 'next'),
          kind: node.kind,
          event: 'fan-in',
          to: node.next,
        });
        current = node.next;
        break;
      }
      default:
        throw new Error(`walkGraph: unknown node kind "${node.kind}" at "${node.id}"`);
    }
  }
}

module.exports = { walkGraph };
