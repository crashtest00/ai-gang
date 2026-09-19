'use strict';

// Filesystem-facing half of workspace pruning: walk the jenkins-data
// workspace root and turn it into the plain-data shape
// retention-policy.js's selectWorkspacesToPrune() consumes, and actually
// remove the directories it says are prunable.
//
// Layout assumed (matches Jenkins' own on-disk convention for the two job
// kinds jenkins.yaml defines — see jenkins/jenkins.yaml):
//   <root>/<multibranchJobName>/<branchOrPRName>/   (e.g. hello-world-pipeline/dev)
//   <root>/<singletonJobName>/                      (e.g. release-candidate)
//
// Which top-level directory is which is NOT guessed from what's on disk —
// a singleton job's own checkout can perfectly well contain subdirectories
// (src/, .git/, node_modules/...), so "has subdirectories" can't
// distinguish it from a multibranch parent. The caller must pass the
// authoritative set of multibranch job names, sourced from Jenkins' own
// job config via the REST API (see lib/jenkins-api.js's
// fetchJenkinsJobState / extractJobState, which derive it from the same
// `jobs[]` shape Jenkins itself uses to mark a multibranch project).

const fs = require('node:fs');
const path = require('node:path');

/**
 * @param {string} root - jenkins-data workspace root, e.g. /var/jenkins_home/workspace
 * @param {object} [opts]
 * @param {Set<string>|string[]} [opts.multibranchJobNames] - top-level job
 *   names known (from the Jenkins API) to be multibranch parents; every
 *   other top-level directory is treated as a singleton job's own workspace
 * @returns {Array<{job: string, branch: string|null, path: string, mtimeMs: number}>}
 */
function discoverWorkspaces(root, opts = {}) {
  if (!fs.existsSync(root)) return [];

  const multibranchJobNames = new Set(opts.multibranchJobNames || []);

  const results = [];
  for (const jobName of fs.readdirSync(root)) {
    const jobPath = path.join(root, jobName);
    const jobStat = fs.statSync(jobPath);
    if (!jobStat.isDirectory()) continue;

    if (multibranchJobNames.has(jobName)) {
      const branchDirs = fs
        .readdirSync(jobPath, { withFileTypes: true })
        .filter((c) => c.isDirectory());
      for (const branchDir of branchDirs) {
        const branchPath = path.join(jobPath, branchDir.name);
        results.push({
          job: jobName,
          branch: branchDir.name,
          path: branchPath,
          mtimeMs: latestMtimeMs(branchPath),
        });
      }
    } else {
      results.push({
        job: jobName,
        branch: null,
        path: jobPath,
        mtimeMs: latestMtimeMs(jobPath),
      });
    }
  }
  return results;
}

/**
 * A workspace's "last used" time is the newest mtime found on any FILE
 * anywhere in its tree, recursively. Deliberately does not fall back to a
 * directory's own mtime while the tree is non-empty: a directory's mtime
 * only reflects "an entry was added or removed directly inside it," not
 * real file-content activity, and on most filesystems is bumped by mere
 * directory creation — using it as the seed would make every freshly
 * checked-out (but otherwise idle) workspace look artificially recent. The
 * directory's own mtime is used only as the last-resort answer for a
 * genuinely empty tree.
 *
 * @param {string} dirPath
 * @returns {number}
 */
function latestMtimeMs(dirPath) {
  let latest = -Infinity;
  let entries;
  try {
    entries = fs.readdirSync(dirPath, { withFileTypes: true });
  } catch {
    return fs.statSync(dirPath).mtimeMs;
  }
  for (const entry of entries) {
    const full = path.join(dirPath, entry.name);
    if (entry.isDirectory()) {
      const nested = latestMtimeMs(full);
      if (nested > latest) latest = nested;
      continue;
    }
    try {
      const stat = fs.statSync(full);
      if (stat.mtimeMs > latest) latest = stat.mtimeMs;
    } catch {
      // Racing deletion or broken symlink — ignore, doesn't change the answer.
    }
  }
  return latest === -Infinity ? fs.statSync(dirPath).mtimeMs : latest;
}

/**
 * Total size in bytes of a directory tree, used only to report how much
 * space a prune run reclaimed — computed BEFORE removal.
 *
 * @param {string} dirPath
 * @returns {number}
 */
function directorySizeBytes(dirPath) {
  let total = 0;
  let entries;
  try {
    entries = fs.readdirSync(dirPath, { withFileTypes: true });
  } catch {
    return 0;
  }
  for (const entry of entries) {
    const full = path.join(dirPath, entry.name);
    if (entry.isSymbolicLink()) continue;
    if (entry.isDirectory()) {
      total += directorySizeBytes(full);
    } else {
      try {
        total += fs.statSync(full).size;
      } catch {
        // ignore races
      }
    }
  }
  return total;
}

/**
 * Remove a workspace directory. Separated to a one-line function so tests
 * (and prune-workspaces.js's dry-run mode) can stub it out without
 * monkey-patching `fs` globally.
 *
 * @param {string} dirPath
 */
function removeWorkspace(dirPath) {
  fs.rmSync(dirPath, { recursive: true, force: true });
}

module.exports = { discoverWorkspaces, directorySizeBytes, removeWorkspace, latestMtimeMs };
