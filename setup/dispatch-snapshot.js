'use strict';

/**
 * Per-dispatch agent-commons snapshot (V5.0 Agent Commons REQ-02, and its
 * Amendment 1: the snapshot carries the per-role definitions and the role
 * handbooks too, and no agent-facing path for one of them names the live mount).
 *
 * The commons is delivered by the read-only bind mount of `setup/` at
 * `/agent-docs`, so an operator's `git pull` stays the whole upgrade and there
 * is one copy on disk. What a session runs from is a copy of it taken at
 * dispatch time: the snapshot is the unit of atomicity and the unit of
 * versioning. A `git pull` landing between two tool invocations inside one
 * task therefore cannot change a tool under a running session, and every
 * session records which commons it ran.
 *
 * What is snapshotted is everything an agent *reads during its session*, not
 * only what is common to every agent (SNAPSHOT_ENTRIES below): `commons/`, the
 * four per-role definitions in `agents/`, and the two role handbooks. The
 * definitions and handbooks are not common — they are per-role — but a session
 * reads them inside itself: ScrumMaster's prompt carries the definition's
 * *path*, so the session opens the file, and `agents/devops-agent.md` makes a
 * mid-session read of `DEVOPS_HANDBOOK_v1.md` mandatory. Left on the mount they
 * were exactly the hazard the snapshot exists to remove (V5.0 audit row 26,
 * Amendment 1). What stays on the mount is what no agent reads as an
 * instruction: the two specifications, `JenkinsConfig.md`, the Jenkinsfile
 * template, `graphs/`, and `subscriber.js` itself.
 *
 * Layout of one dispatch, rooted at a directory this module mints:
 *
 *   <root>/dispatch-XXXXXX/commons/               AIGANG_COMMONS_DIR
 *   <root>/dispatch-XXXXXX/commons/tools/         first on the session's PATH
 *   <root>/dispatch-XXXXXX/commons/skills/        installed into ~/.claude/skills
 *   <root>/dispatch-XXXXXX/agents/<role>.md       the per-role definitions
 *   <root>/dispatch-XXXXXX/DEVOPS_HANDBOOK_v1.md  the role handbooks
 *   <root>/dispatch-XXXXXX/DESKTOP_HANDBOOK_v1.md
 *   <root>/dispatch-XXXXXX/state/                 per-session state, beside it
 *
 * The dispatch directory is deliberately a *partial mirror of the mount*: each
 * snapshotted entry keeps the name it has under `/agent-docs`. That is what
 * makes the path rewrite one rule with no table — `/agent-docs/<entry>` becomes
 * `<dispatchDir>/<entry>` — for the prompt ScrumMaster rendered and for the
 * handbook path a definition names inside itself (see rewriteAgentPaths).
 * `state/` is the one directory in here that is not a mirror of anything; no
 * `state` entry exists under `/agent-docs`, so nothing rewrites onto it.
 *
 * `<root>` is under the OS temporary directory, never under `/workspace`:
 * `/workspace` is the project's git repository and holds no platform state.
 * Nothing writes into the snapshotted entries once the session starts — the
 * hash is the description of what the session ran, so it has to stay true for
 * the session's whole life. A tool that needs per-session state writes it in
 * the sibling `state/` directory, which the whole dispatch directory takes with
 * it when the session ends. `state/` is reachable from the environment without
 * a further variable: it is
 * `path.join(path.dirname(AIGANG_COMMONS_DIR), 'state')`, and keeping
 * `AIGANG_COMMONS_DIR` at `<dispatchDir>/commons` is what keeps that derivation
 * — the constructor's contract in `commons/tools/a2a-submit.js` — true.
 *
 * This module lives in `setup/` beside `subscriber.js`, the runtime that calls
 * it, and not in `setup/commons/` — it is not something an agent runs, and the
 * commons holds only what every agent shares. Keeping it out also keeps it out
 * of the hash it computes.
 */

const crypto = require('crypto');
const fs = require('fs');
const os = require('os');
const path = require('path');

// The mount every project container sees: docker-compose mounts `setup/` at
// `/agent-docs` read-only (`Docker Templates/docker-compose.template:28`,
// `scripts/init-project.sh:703`). When an environment arrives that the host
// cannot mount, this is the one value that changes.
//
// It must stay a plain tree of regular files and directories: it holds no
// symlink today, and a symlink here would quietly defeat the snapshot (V5.0
// audit row 33). `cpSync`'s `dereference: true` below does not reach an entry
// *under* the tree — at Node 22 a symlink is copied as a symlink either way,
// with its target rewritten to an absolute path back into the source — so a
// symlinked tool in the snapshot still resolves through `/agent-docs`, which is
// exactly the live mount a `git pull` changes under the running session. It is
// also invisible to `listFiles` below, whose `entry.isFile()` is false for a
// link, so the version stamp does not cover it and a dangling one is silently
// absent rather than an error. `dereference: true` stays because it is what
// makes a symlinked *source root* work; it is not a defence against this.
const MOUNT_SOURCE = '/agent-docs';

// The path prefix an agent is *given* for a mounted file — in ScrumMaster's
// prompt (`services/scrummaster/config/agents.json`'s `definitionPath` values,
// printed by `prompt.js`) and inside the role definitions themselves. It is the
// container-side constant the rewrite keys on, and it stays `/agent-docs` even
// when `source` below is a test directory: what is being rewritten is the text
// those two places already contain.
const MOUNT_PATH = '/agent-docs';

// Everything an agent reads during its session, as top-level entries of the
// mount. Every one of them must be present: a dispatch that cannot carry one
// cannot make REQ-02's promise about it, and failing the dispatch loudly is
// better than a session reading a file that a `git pull` can change under it.
const SNAPSHOT_ENTRIES = ['commons', 'agents', 'DEVOPS_HANDBOOK_v1.md', 'DESKTOP_HANDBOOK_v1.md'];

// Outside the project's working tree by construction.
const SNAPSHOT_ROOT = path.join(os.tmpdir(), 'aigang-dispatch');

// Where Claude Code discovers personal skills. One directory per container,
// not per session, which the serial dispatch loop is what makes safe.
const SKILLS_HOME = path.join(os.homedir(), '.claude', 'skills');

// Every regular file under `dir`, as slash-separated paths relative to `dir`,
// sorted, so the walk order is the same on every host.
function listFiles(dir, prefix = '') {
  const out = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const rel = prefix ? `${prefix}/${entry.name}` : entry.name;
    if (entry.isDirectory()) {
      out.push(...listFiles(path.join(dir, entry.name), rel));
    } else if (entry.isFile()) {
      out.push(rel);
    }
  }
  return out.sort();
}

// Every directory under `dir`, in the same form and order as listFiles. Kept
// separate so listFiles stays "the files", which is what markExecutables and
// the hash's content loop both want.
function listDirectories(dir, prefix = '') {
  const out = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    if (!entry.isDirectory()) continue;
    const rel = prefix ? `${prefix}/${entry.name}` : entry.name;
    out.push(rel, ...listDirectories(path.join(dir, entry.name), rel));
  }
  return out.sort();
}

// The snapshotted entries' files and directories, as relative paths under the
// dispatch directory. Named entries rather than "everything under `dir`",
// because `state/` sits in the same directory and is the session's to write:
// it is not part of what the session *read*, so it is not hashed and not
// rewritten.
function snapshotFiles(dir, entries) {
  const out = [];
  for (const rel of entries) {
    const full = path.join(dir, rel);
    if (fs.statSync(full).isDirectory()) out.push(...listFiles(full, rel));
    else out.push(rel);
  }
  return out.sort();
}

function snapshotDirectories(dir, entries) {
  const out = [];
  for (const rel of entries) {
    const full = path.join(dir, rel);
    if (!fs.statSync(full).isDirectory()) continue;
    out.push(rel, ...listDirectories(full, rel));
  }
  return out.sort();
}

// SHA-256 over the snapshot's contents: every directory name, then for each
// file, in sorted relative-path order, the path and the SHA-256 of its bytes.
// Content-derived, so it needs no maintenance and cannot drift, and equal for
// two sessions that ran the same commons — which is what a session record has
// to be able to say. It covers every snapshotted entry, so it differs when a
// role definition or a handbook differs and `AIGANG_COMMONS_VERSION` describes
// everything the session read (Amendment 1). File modes are deliberately
// outside the hash: `markExecutables` below sets them from the content itself,
// so they add nothing and a source tree that lost its executable bits still
// hashes to the same value.
//
// The directory names are in it because a directory can be observable while
// holding no file: an empty `skills/` and an absent `skills/` take different
// branches in `installSkills` (its `existsSync`), so a stamp that could not
// tell them apart would describe two sessions that ran differently as having
// run the same commons (V5.0 audit row 32). The two section labels keep a
// directory's name from ever colliding with a file's.
function hashSnapshot(dir, entries) {
  const digest = crypto.createHash('sha256');
  digest.update('directories\n', 'utf8');
  for (const rel of snapshotDirectories(dir, entries)) {
    digest.update(rel, 'utf8');
    digest.update('\n');
  }
  digest.update('files\n', 'utf8');
  for (const rel of snapshotFiles(dir, entries)) {
    digest.update(rel, 'utf8');
    digest.update('\0');
    digest.update(crypto.createHash('sha256').update(fs.readFileSync(path.join(dir, rel))).digest('hex'), 'utf8');
    digest.update('\n');
  }
  return digest.digest('hex');
}

// Mark every copied tool executable, so a tool on PATH is invoked by name and
// its shebang runs it — no `node` and no path. A file that starts with `#!`
// says it is meant to be executed; the shared modules a tool requires do not,
// and are left alone. Only the commons holds executables: a role definition and
// a handbook are read, never run.
function markExecutables(dir) {
  const marked = [];
  for (const rel of listFiles(dir)) {
    const file = path.join(dir, rel);
    const head = Buffer.alloc(2);
    const fd = fs.openSync(file, 'r');
    let read = 0;
    try {
      read = fs.readSync(fd, head, 0, 2, 0);
    } finally {
      fs.closeSync(fd);
    }
    if (read === 2 && head[0] === 0x23 && head[1] === 0x21) {
      fs.chmodSync(file, 0o755);
      marked.push(rel);
    }
  }
  return marked;
}

/**
 * Point a mount path at this session's own snapshot: every `/agent-docs/<entry>`
 * for a snapshotted entry becomes `<dispatchDir>/<entry>`.
 *
 * This is REQ-02's "every path an agent is given for one of them resolves
 * inside that session's own snapshot rather than the live mount", applied to
 * the two places such a path reaches an agent:
 *
 *   - the dispatch prompt, whose first lines are ScrumMaster's fixed
 *     `definitionPath` — a value in `services/scrummaster/config/agents.json`,
 *     which this feature does not change, so the subscriber rewrites the prompt
 *     it received before spawning the session (the mechanism REQ-02 names);
 *   - the snapshotted documents themselves, because a role definition names the
 *     handbook it makes mandatory by absolute mount path
 *     (`agents/devops-agent.md`).
 *
 * A path under an entry that is *not* snapshotted is left alone, because the
 * mount is still where it lives and still where it should be read from: the two
 * specifications, `JenkinsConfig.md`, the Jenkinsfile template, `graphs/` and
 * `subscriber.js` (§4). So is a bare `/agent-docs` with no entry after it: it
 * names the mount as a directory, which still exists, is still read-only, and
 * still holds those files.
 *
 * Substitution is by split/join rather than String.replace, which would eat a
 * `$$` in the replacement — a temporary directory name can hold any character
 * `mkdtemp` produces.
 */
function rewriteAgentPaths(text, snapshot) {
  let out = text;
  for (const entry of snapshot.entries) {
    out = out.split(`${MOUNT_PATH}/${entry}`).join(path.join(snapshot.dispatchDir, entry));
  }
  return out;
}

// Rewrite the mount paths inside the snapshotted documents. Markdown only:
// those are the files an agent is given as instructions, and they are where an
// instruction to read another snapshotted file lives. A comment in a tool's
// source is not a path an agent is given, and one of them
// (`commons/tools/gateway-publish.js`) documents Jenkins' own invocation, which
// REQ-04 keeps on the mount on purpose.
//
// Runs after the hash is taken, and that order is deliberate: what it
// substitutes in is this dispatch's own directory name, which `mkdtemp` makes
// different every time. Hashing afterwards would give two dispatches of an
// identical mount two different stamps and destroy the one thing the stamp is
// for — saying that two sessions ran the same commons. The stamp therefore
// describes the mount content the session read; the rewrite carries no
// information it could describe.
function rewriteSnapshottedDocs(snapshot) {
  const rewritten = [];
  for (const rel of snapshotFiles(snapshot.dispatchDir, snapshot.entries)) {
    if (!rel.endsWith('.md')) continue;
    const file = path.join(snapshot.dispatchDir, rel);
    const before = fs.readFileSync(file, 'utf8');
    const after = rewriteAgentPaths(before, snapshot);
    if (after === before) continue;
    fs.writeFileSync(file, after);
    rewritten.push(rel);
  }
  return rewritten;
}

/**
 * Take one dispatch's snapshot. Returns the paths the caller needs plus the
 * version stamp. Every dispatch gets its own directory — `mkdtemp` is what
 * guarantees two successive dispatches in one container cannot share one.
 */
function createSnapshot({ source = MOUNT_SOURCE, root = SNAPSHOT_ROOT, entries = SNAPSHOT_ENTRIES } = {}) {
  fs.mkdirSync(root, { recursive: true });
  const dispatchDir = fs.mkdtempSync(path.join(root, 'dispatch-'));
  // `mkdtemp` has already minted the directory, so everything after it runs
  // under this guard: a copy that throws — an absent mount, ENOSPC, a dangling
  // symlink — used to leave the whole directory behind, once per attempt and so
  // three times per task at the subscriber's MAX_ATTEMPTS. subscriber.js's own
  // guarded `removeSnapshot` cannot reach it, because there is no snapshot to
  // pass it: this call never returned (V5.0 audit row 27).
  try {
    // An absent or unreadable mount fails here, and an incomplete one fails on
    // the next line: either way the dispatch reports commons_snapshot_failed
    // rather than running a session whose instructions can change under it.
    const present = new Set(fs.readdirSync(source));
    const missing = entries.filter(entry => !present.has(entry));
    if (missing.length) {
      throw Object.assign(
        new Error(`ENOENT: the mount ${source} is missing ${missing.join(', ')}`),
        { code: 'ENOENT' }
      );
    }

    const commonsDir = path.join(dispatchDir, 'commons');
    const stateDir = path.join(dispatchDir, 'state');
    for (const entry of entries) {
      fs.cpSync(path.join(source, entry), path.join(dispatchDir, entry), { recursive: true, dereference: true });
    }
    fs.mkdirSync(stateDir, { recursive: true });
    const executables = markExecutables(commonsDir);
    const snapshot = {
      dispatchDir,
      commonsDir,
      toolsDir: path.join(commonsDir, 'tools'),
      stateDir,
      entries: [...entries],
      executables,
      version: hashSnapshot(dispatchDir, entries),
    };
    snapshot.rewritten = rewriteSnapshottedDocs(snapshot);
    return snapshot;
  } catch (err) {
    fs.rmSync(dispatchDir, { recursive: true, force: true });
    throw err;
  }
}

/**
 * Replace the contents of the session user's skills directory with this
 * snapshot's `skills/`, wholesale. That directory is where Claude Code
 * discovers skills and nothing else installs any there, so a replacement — not
 * a merge — is what makes the session's skills exactly the snapshot's.
 */
function installSkills(snapshot, skillsHome = SKILLS_HOME) {
  const from = path.join(snapshot.commonsDir, 'skills');
  fs.rmSync(skillsHome, { recursive: true, force: true });
  fs.mkdirSync(skillsHome, { recursive: true });
  if (fs.existsSync(from)) {
    fs.cpSync(from, skillsHome, { recursive: true, dereference: true });
  }
  return skillsHome;
}

/**
 * The commons half of the session's environment: the snapshot's tools first on
 * PATH, the snapshot directory, and the version stamp.
 *
 * Three values, and no fourth for the definitions and handbooks: their paths
 * reach the session in its prompt and in the documents themselves, already
 * rewritten, and REQ-03's acceptance counts the session's variables at seven.
 */
function sessionEnv(snapshot, basePath = process.env.PATH) {
  return {
    PATH: basePath ? `${snapshot.toolsDir}:${basePath}` : snapshot.toolsDir,
    AIGANG_COMMONS_DIR: snapshot.commonsDir,
    AIGANG_COMMONS_VERSION: snapshot.version,
  };
}

// The session has ended: the snapshot and any per-session state beside it go
// with it.
function removeSnapshot(snapshot) {
  fs.rmSync(snapshot.dispatchDir, { recursive: true, force: true });
}

/**
 * Remove every dispatch directory sitting in `<root>`, and report their names.
 *
 * A subscriber that is killed rather than shut down never reaches
 * `removeSnapshot`, and pm2 restarts it — so its snapshot, a full copy of the
 * commons, would sit in the container's temporary directory for the container's
 * life, one per kill (V5.0 audit row 27). This is called once by the subscriber
 * before its first consumer starts, and only there: dispatches are serial and
 * this process is the only writer of `<root>`, so at that moment nothing in it
 * can belong to a live session. Calling it later would delete the snapshot of
 * the session that is running.
 */
function pruneSnapshotRoot(root = SNAPSHOT_ROOT) {
  let entries;
  try {
    entries = fs.readdirSync(root, { withFileTypes: true });
  } catch (err) {
    if (err.code === 'ENOENT') return [];
    throw err;
  }
  const removed = [];
  for (const entry of entries) {
    if (!entry.isDirectory() || !entry.name.startsWith('dispatch-')) continue;
    fs.rmSync(path.join(root, entry.name), { recursive: true, force: true });
    removed.push(entry.name);
  }
  return removed.sort();
}

module.exports = { createSnapshot, installSkills, sessionEnv, rewriteAgentPaths, removeSnapshot, pruneSnapshotRoot };
