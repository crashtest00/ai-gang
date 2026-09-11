'use strict';

const { formatNodeRef, formatEdgeRef } = require('./addressing');

const TERMINAL_STATUSES = new Set(['done', 'failed', 'cancelled', 'paused-for-human']);

/**
 * Coordination mechanism (parallel-branch-execution.md).
 *
 * This is a deterministic, non-decision-making mechanism — not an agent,
 * not a PRD §3 participating actor (§4, "Coordination mechanism role and
 * authority"). It:
 *   - decides collision-safety among already-independent candidates
 *     (REQ-02/REQ-03/REQ-04),
 *   - dispatches one local process/session per collision-safe branch
 *     (REQ-05/REQ-06),
 *   - tracks each branch to a terminal or paused-for-human outcome in its
 *     own local record (REQ-07 through REQ-14),
 * and nothing else. It deliberately has no method that calls out to
 * canonical-work-model.md, Jira, or Redis/Streams (REQ-11), and no method
 * that edits a graph document (REQ-01).
 *
 * The local tracking record (REQ-12) lives only in this instance's memory
 * for the duration of a walk — it is not required to survive past it, and
 * is not a canonical work item.
 */
class Coordination {
  constructor() {
    /** @type {Map<string, object>} keyed by `${groupId}::${address}` */
    this._records = new Map();
    /** @type {Map<string, Set<string>>} groupId -> set of addresses */
    this._groups = new Map();
  }

  // ---------------------------------------------------------------------
  // Sourcing concurrency candidates (REQ-02): the only two allowed sources.
  // ---------------------------------------------------------------------

  /**
   * Source candidates from distinct, non-cross-referencing graph entry
   * points (REQ-02(a)). Each input is { graphDoc, writes? } — graphDoc is
   * a full, already-validated graph document; its own `entry` node is the
   * candidate. Throws if any two candidates share a graph_id and one's
   * entry node is reachable from another's (REQ-02 acceptance: "A graph
   * entry point reachable from another candidate's walk never appears in
   * the same candidate set as that candidate").
   */
  static sourceEntryPointCandidates(entries) {
    const candidates = entries.map(({ graphDoc, writes }) => ({
      address: formatNodeRef(graphDoc.graph_id, graphDoc.entry),
      graphId: graphDoc.graph_id,
      nodeId: graphDoc.entry,
      writes: writes || null,
      graphDoc,
    }));
    assertNoCrossReferencingCandidates(candidates);
    return candidates;
  }

  /**
   * Source candidates from the branches declared together at one
   * fan-out node (REQ-02(b)). `graphDoc` must contain `fanOutNodeId` as a
   * `fan-out` node; each of its declared branches becomes one candidate,
   * addressed as `<graph_id>#<node_id>:<branch_id>` (REQ-05).
   */
  static sourceFanOutCandidates(graphDoc, fanOutNodeId, writesByBranchId = {}) {
    const fanOutNode = graphDoc.nodes.find((n) => n.id === fanOutNodeId);
    if (!fanOutNode || fanOutNode.kind !== 'fan-out') {
      throw new Error(`sourceFanOutCandidates: "${fanOutNodeId}" is not a fan-out node in "${graphDoc.graph_id}"`);
    }
    const candidates = fanOutNode.branches.map((branch) => ({
      address: formatEdgeRef(graphDoc.graph_id, fanOutNode.id, branch.branch_id),
      graphId: graphDoc.graph_id,
      nodeId: branch.to,
      branchId: branch.branch_id,
      writes: writesByBranchId[branch.branch_id] || null,
      graphDoc,
    }));
    assertNoCrossReferencingCandidates(candidates);
    return candidates;
  }

  // ---------------------------------------------------------------------
  // Collision-safety (REQ-03/REQ-04)
  // ---------------------------------------------------------------------

  /** Two candidates are collision-safe iff their declared write-scopes are
   * pairwise disjoint in files and services. A candidate with no declared
   * write-scope is never collision-safe with anything — it fails closed to
   * the sequential path (REQ-03). */
  static isCollisionSafe(a, b) {
    if (!hasWriteScope(a) || !hasWriteScope(b)) return false;
    const filesOverlap = a.writes.files.some((f) => b.writes.files.includes(f));
    const servicesOverlap = a.writes.services.some((s) => b.writes.services.includes(s));
    return !filesOverlap && !servicesOverlap;
  }

  /**
   * Partition a candidate set into concurrent-eligible groups (each group
   * pairwise collision-safe internally) and a sequential list (candidates
   * with no declared write-scope, per REQ-03). Two candidate groups never
   * run concurrently with each other by construction — this method only
   * describes what's safe to run together within one dispatch, callers
   * dispatch each group independently, one after another, as needed.
   */
  static partitionCollisionSafe(candidates) {
    const concurrent = candidates.filter(hasWriteScope);
    const sequential = candidates.filter((c) => !hasWriteScope(c));

    const groups = [];
    for (const candidate of concurrent) {
      const group = groups.find((g) => g.every((member) => Coordination.isCollisionSafe(member, candidate)));
      if (group) {
        group.push(candidate);
      } else {
        groups.push([candidate]);
      }
    }
    return { concurrentGroups: groups, sequential };
  }

  // ---------------------------------------------------------------------
  // Dispatch and tracking (REQ-05 through REQ-14)
  // ---------------------------------------------------------------------

  /**
   * Dispatch every candidate in `candidates` concurrently under one fan-out
   * group id, running `runBranch(candidate)` for each. `runBranch` MUST
   * return a Promise resolving to one of:
   *   - { status: 'done' }
   *   - { status: 'paused-for-human', reason }   (REQ-09)
   *   - { status: 'failed', reason }              (REQ-08, second case)
   * or reject (treated as an execution-layer failure, REQ-08 second case).
   *
   * This is local-process dispatch (REQ-06): `runBranch` is supplied by the
   * caller and may spawn an actual OS process, an async agent session, or
   * (in tests) a scripted stand-in — this module has no Redis/Streams
   * dependency and makes no assumption about what `runBranch` is backed by.
   *
   * A candidate's own graph-modeled remediation path (missing prerequisite
   * with a defined remediation node) is handled entirely inside `runBranch`
   * (typically a walkGraph call) before it ever resolves — this method only
   * ever sees the branch's final outcome, never a mid-walk remediation
   * step, consistent with REQ-08's first case producing no failure record
   * at all.
   *
   * One branch's failure or timeout never aborts a sibling (REQ-07):
   * failures are caught per-branch and never rethrown across `Promise.all`.
   */
  async dispatchGroup(groupId, candidates, runBranch, opts = {}) {
    const timeoutMs = opts.timeoutMs ?? 30000;
    const maxAttempts = opts.maxAttempts ?? 1;

    const runOne = async (candidate) => {
      this._recordDispatch(groupId, candidate.address);
      this._setStatus(groupId, candidate.address, 'running');
      let attempt = 0;
      // eslint-disable-next-line no-constant-condition
      while (true) {
        attempt++;
        try {
          const result = await withTimeout(runBranch(candidate), timeoutMs);
          if (result && result.status === 'paused-for-human') {
            this._setStatus(groupId, candidate.address, 'paused-for-human', {
              reason: result.reason || null,
              attempts: attempt,
            });
            return;
          }
          if (result && result.status === 'done') {
            this._setStatus(groupId, candidate.address, 'done', { attempts: attempt });
            return;
          }
          // Anything else (including {status:'failed'}) is a REQ-08
          // second-case execution-layer failure, subject to retry.
          if (attempt < maxAttempts) continue;
          this._setStatus(groupId, candidate.address, 'failed', {
            reason: (result && result.reason) || 'branch reported failure',
            attempts: attempt,
          });
          return;
        } catch (err) {
          // Timeout (REQ-13) or a thrown execution-layer error (REQ-08).
          if (attempt < maxAttempts) continue;
          this._setStatus(groupId, candidate.address, 'failed', {
            reason: err.message,
            attempts: attempt,
          });
          return;
        }
      }
    };

    // REQ-07 sibling isolation: runOne never rejects, so Promise.all never
    // short-circuits on one branch's failure.
    await Promise.all(candidates.map(runOne));
    return this.getGroupStatus(groupId);
  }

  _recordDispatch(groupId, address) {
    const key = recordKey(groupId, address);
    if (!this._records.has(key)) {
      this._records.set(key, { groupId, address, status: 'dispatched', attempts: 0 });
    }
    if (!this._groups.has(groupId)) this._groups.set(groupId, new Set());
    this._groups.get(groupId).add(address);
  }

  _setStatus(groupId, address, status, extra = {}) {
    const key = recordKey(groupId, address);
    const existing = this._records.get(key) || { groupId, address, attempts: 0 };
    this._records.set(key, { ...existing, ...extra, status });
  }

  /** REQ-10: a group is complete only when every dispatched branch has
   * reached a terminal or paused-for-human status. A group with no
   * dispatched branches yet is not complete. */
  isGroupComplete(groupId) {
    const addresses = this._groups.get(groupId);
    if (!addresses || addresses.size === 0) return false;
    for (const address of addresses) {
      const rec = this._records.get(recordKey(groupId, address));
      if (!rec || !TERMINAL_STATUSES.has(rec.status)) return false;
    }
    return true;
  }

  /** REQ-14: inspectable fan-out state — every branch's dispatched
   * address, current/terminal status, and (if paused/failed) its reason. */
  getGroupStatus(groupId) {
    const addresses = this._groups.get(groupId) || new Set();
    const branches = [...addresses].map((address) => ({ ...this._records.get(recordKey(groupId, address)) }));
    return { groupId, complete: this.isGroupComplete(groupId), branches };
  }
}

function hasWriteScope(candidate) {
  return Boolean(candidate.writes && Array.isArray(candidate.writes.files) && Array.isArray(candidate.writes.services));
}

function recordKey(groupId, address) {
  return `${groupId}::${address}`;
}

function withTimeout(promise, ms) {
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`branch timed out after ${ms}ms`)), ms);
  });
  return Promise.race([Promise.resolve(promise), timeout]).finally(() => clearTimeout(timer));
}

function assertNoCrossReferencingCandidates(candidates) {
  const seen = [];
  for (const candidate of candidates) {
    for (const other of seen) {
      if (candidate.graphId === other.graphId && candidate.graphDoc) {
        if (isReachable(candidate.graphDoc, other.nodeId, candidate.nodeId) ||
            isReachable(candidate.graphDoc, candidate.nodeId, other.nodeId)) {
          throw new Error(
            `sourceCandidates: "${other.address}" and "${candidate.address}" are not independent — one is reachable from the other`
          );
        }
      }
    }
    seen.push(candidate);
  }
}

function isReachable(graphDoc, fromId, toId) {
  if (fromId === toId) return false; // a node is not "reachable from itself" for this purpose
  const nodesById = new Map(graphDoc.nodes.map((n) => [n.id, n]));
  const visited = new Set();
  const stack = [fromId];
  while (stack.length > 0) {
    const id = stack.pop();
    if (id === toId) return true;
    if (visited.has(id)) continue;
    visited.add(id);
    const node = nodesById.get(id);
    if (!node) continue;
    const targets =
      node.kind === 'decision' || node.kind === 'fan-out'
        ? (node.branches || []).map((b) => b.to)
        : node.next
        ? [node.next]
        : [];
    for (const t of targets) if (t) stack.push(t);
  }
  return false;
}

module.exports = { Coordination };
