'use strict';

// Pure decision logic for Jenkins workspace/Docker-cache pruning
// (the jenkins-cache-retention design REQ-01..REQ-04).
//
// Nothing in this file touches the filesystem, shells out to `docker`, or
// calls the Jenkins REST API. Every function takes plain data in and
// returns plain data out, so the prune/threshold decisions are unit
// testable without a real jenkins-data volume, a real Docker daemon, or a
// running Jenkins controller. The impure orchestration (walking
// jenkins-data, calling the Jenkins API to find in-progress builds,
// actually deleting directories, actually shelling out to `docker`) lives
// in prune-workspaces.js / prune-docker-cache.js, which call into this
// module for every decision.

const DEFAULT_MAX_AGE_DAYS = 14;
const DEFAULT_MAX_KEEP_PER_JOB = 5;
const DEFAULT_DOCKER_UNTIL_HOURS = 72;
const DEFAULT_HIGH_WATERMARK_PERCENT = 85;
const DEFAULT_LOW_WATERMARK_PERCENT = 70;
const DEFAULT_DISK_CHECK_INTERVAL_MINUTES = 30;

const DAY_MS = 24 * 60 * 60 * 1000;

/**
 * Decide which on-disk job workspaces are eligible for pruning (REQ-01),
 * with in-progress builds always excluded no matter what (REQ-04).
 *
 * @param {Array<{job: string, branch: string|null, path: string, mtimeMs: number, building: boolean}>} workspaces
 * @param {object} [opts]
 * @param {number} [opts.now] - epoch ms "now" reference (injectable for tests)
 * @param {number} [opts.maxAgeDays]
 * @param {number} [opts.maxKeepPerJob]
 * @returns {{keep: Array, prune: Array}} each entry annotated with `reason`
 */
function selectWorkspacesToPrune(workspaces, opts = {}) {
  const now = opts.now ?? Date.now();
  const maxAgeMs = (opts.maxAgeDays ?? DEFAULT_MAX_AGE_DAYS) * DAY_MS;
  const maxKeepPerJob = opts.maxKeepPerJob ?? DEFAULT_MAX_KEEP_PER_JOB;

  const byJob = new Map();
  for (const ws of workspaces) {
    if (!byJob.has(ws.job)) byJob.set(ws.job, []);
    byJob.get(ws.job).push(ws);
  }

  const keep = [];
  const prune = [];

  for (const [, entries] of byJob) {
    // Most recently used first, so "rank" below 0-indexes from newest.
    const ranked = [...entries].sort((a, b) => b.mtimeMs - a.mtimeMs);

    ranked.forEach((ws, rank) => {
      const ageMs = now - ws.mtimeMs;
      const overAge = ageMs > maxAgeMs;
      const overCount = rank >= maxKeepPerJob;

      // REQ-04 is a hard override: an in-progress build's workspace is
      // never eligible, regardless of age or how many superseded
      // workspaces its job has — checked last, wins over every other rule.
      if (ws.building) {
        keep.push({ ...ws, reason: 'active-build' });
        return;
      }

      if (overAge || overCount) {
        prune.push({
          ...ws,
          reason: overAge && overCount ? 'over-age-and-over-count' : overAge ? 'over-age' : 'over-count',
        });
      } else {
        keep.push({ ...ws, reason: 'within-retention' });
      }
    });
  }

  return { keep, prune };
}

/**
 * Build the argv for the nightly Docker image/layer cache prune (REQ-02).
 * Deliberately does NOT pass `-a`: `docker system prune` without `-a` only
 * ever touches dangling images, unused (i.e. not attached to any
 * container, running or stopped) build cache and networks, and stopped
 * containers — an image or cache layer backing a currently running build's
 * container is never a candidate, which is what gives REQ-04 its Docker-side
 * guarantee for free, from Docker's own semantics, rather than something
 * this script has to reimplement.
 *
 * @param {object} [opts]
 * @param {number} [opts.untilHours]
 * @returns {string[]} argv, e.g. ['docker', 'system', 'prune', '-f', '--filter', 'until=72h']
 */
function buildDockerPruneCommand(opts = {}) {
  const untilHours = opts.untilHours ?? DEFAULT_DOCKER_UNTIL_HOURS;
  if (!Number.isFinite(untilHours) || untilHours <= 0) {
    throw new Error(`buildDockerPruneCommand: untilHours must be a positive number, got ${untilHours}`);
  }
  return ['docker', 'system', 'prune', '-f', '--filter', `until=${untilHours}h`];
}

/**
 * Parse the reclaimed-space line `docker system prune` prints, e.g.
 * "Total reclaimed space: 1.234GB" -> 1234000000 (approx, decimal units,
 * matching Docker's own CLI convention). Returns null if the expected line
 * isn't present (defensive — output format is not a stable contract).
 *
 * @param {string} stdout
 * @returns {number|null} bytes
 */
function parseDockerReclaimedBytes(stdout) {
  const match = /Total reclaimed space:\s*([\d.]+)\s*([kKmMgGtT]?)B/.exec(stdout);
  if (!match) return null;
  const value = parseFloat(match[1]);
  const unit = match[2].toUpperCase();
  const multipliers = { '': 1, K: 1e3, M: 1e6, G: 1e9, T: 1e12 };
  return Math.round(value * multipliers[unit]);
}

/**
 * REQ-03's hysteresis: start (or continue) an out-of-band sweep once usage
 * reaches the high watermark, and keep sweeping — across repeated
 * evaluations — until it drops back below the low watermark. Exposed as a
 * pure state-transition function: caller supplies the current reading and
 * whether a sweep is already in progress, gets back whether a sweep should
 * be running now.
 *
 * Boundary: "reaches 85%" is inclusive (>=); "drops back below 70%" is
 * exclusive on the low side (< 70, not <=), matching the spec's wording.
 *
 * @param {number} usedPercent
 * @param {boolean} currentlySweeping
 * @param {object} [opts]
 * @param {number} [opts.highWatermarkPercent]
 * @param {number} [opts.lowWatermarkPercent]
 * @returns {boolean} whether a sweep should be (or remain) active
 */
function shouldSweep(usedPercent, currentlySweeping, opts = {}) {
  const high = opts.highWatermarkPercent ?? DEFAULT_HIGH_WATERMARK_PERCENT;
  const low = opts.lowWatermarkPercent ?? DEFAULT_LOW_WATERMARK_PERCENT;

  if (usedPercent >= high) return true;
  if (currentlySweeping && usedPercent >= low) return true;
  return false;
}

/**
 * Given a sequence of disk-usage readings taken during one sweep run,
 * decide when to stop iterating: either usage has dropped below the low
 * watermark, or a max-iteration safety cap is hit (a real "swept but
 * couldn't get under target" case — nothing left that's safe to prune —
 * which the caller should log, not loop on forever).
 *
 * @param {number} usedPercent
 * @param {number} iterationCount - iterations already performed this run
 * @param {object} [opts]
 * @param {number} [opts.lowWatermarkPercent]
 * @param {number} [opts.maxIterations]
 * @returns {{done: boolean, reason: 'below-target'|'max-iterations'|'continue'}}
 */
function evaluateSweepIteration(usedPercent, iterationCount, opts = {}) {
  const low = opts.lowWatermarkPercent ?? DEFAULT_LOW_WATERMARK_PERCENT;
  const maxIterations = opts.maxIterations ?? 5;

  if (usedPercent < low) return { done: true, reason: 'below-target' };
  if (iterationCount >= maxIterations) return { done: true, reason: 'max-iterations' };
  return { done: false, reason: 'continue' };
}

module.exports = {
  DEFAULT_MAX_AGE_DAYS,
  DEFAULT_MAX_KEEP_PER_JOB,
  DEFAULT_DOCKER_UNTIL_HOURS,
  DEFAULT_HIGH_WATERMARK_PERCENT,
  DEFAULT_LOW_WATERMARK_PERCENT,
  DEFAULT_DISK_CHECK_INTERVAL_MINUTES,
  DAY_MS,
  selectWorkspacesToPrune,
  buildDockerPruneCommand,
  parseDockerReclaimedBytes,
  shouldSweep,
  evaluateSweepIteration,
};
