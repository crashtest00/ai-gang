'use strict';

// Detection logic for the branch-tracking/stale-ref branch-point class
// (graph-process-engine.md REQ-13): does the current branch's upstream
// tracking ref still match the remote's actual current default branch,
// or has a server-side rename left it stale?
//
// `exec` is an injectable command runner — `(cmd) => Promise<string>` (or a
// sync `(cmd) => string`) returning stdout — so tests never have to shell
// out to a real git repository; production callers pass something backed
// by child_process.
//
// Returns one of: "matches" | "stale" | "probe-error"

async function checkBranchTracking(exec) {
  try {
    const upstream = (await exec('git rev-parse --abbrev-ref --symbolic-full-name @{u}')).trim();
    const remoteHead = (await exec('git symbolic-ref refs/remotes/origin/HEAD')).trim();
    // "refs/remotes/origin/HEAD" -> "origin/main"
    const remoteDefault = remoteHead.replace(/^refs\/remotes\//, '');
    return upstream === remoteDefault ? 'matches' : 'stale';
  } catch {
    return 'probe-error';
  }
}

module.exports = { checkBranchTracking };
