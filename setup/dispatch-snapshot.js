'use strict';

/**
 * Per-dispatch agent-commons snapshot (V5.0 Agent Commons REQ-02).
 *
 * The commons is delivered by the read-only bind mount of `setup/` at
 * `/agent-docs`, so an operator's `git pull` stays the whole upgrade and there
 * is one copy on disk. What a session runs from is a copy of it taken at
 * dispatch time: the snapshot is the unit of atomicity and the unit of
 * versioning. A `git pull` landing between two tool invocations inside one
 * task therefore cannot change a tool under a running session, and every
 * session records which commons it ran.
 *
 * Layout of one dispatch, rooted at a directory this module mints:
 *
 *   <root>/dispatch-XXXXXX/commons/   the snapshot — AIGANG_COMMONS_DIR
 *   <root>/dispatch-XXXXXX/commons/tools/   first on the session's PATH
 *   <root>/dispatch-XXXXXX/state/     per-session state, beside the snapshot
 *
 * `<root>` is under the OS temporary directory, never under `/workspace`:
 * `/workspace` is the project's git repository and holds no platform state.
 * Nothing writes into `commons/` — the hash is the description of what the
 * session ran, so it has to stay true for the session's whole life. A tool
 * that needs per-session state writes it in the sibling `state/` directory,
 * which the whole dispatch directory takes with it when the session ends.
 * `state/` is reachable from the environment without a further variable:
 * it is `path.join(path.dirname(AIGANG_COMMONS_DIR), 'state')`.
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

// The mounted commons every project container sees (docker-compose mounts
// `setup/` at `/agent-docs` read-only). When an environment arrives that the
// host cannot mount, this is the one value that changes.
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
const COMMONS_SOURCE = '/agent-docs/commons';

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

// SHA-256 over the snapshot's contents: every directory name, then for each
// file, in sorted relative-path order, the path and the SHA-256 of its bytes.
// Content-derived, so it needs no maintenance and cannot drift, and equal for
// two sessions that ran the same commons — which is what a session record has
// to be able to say. File modes are deliberately outside the hash:
// `markExecutables` below sets them from the content itself, so they add
// nothing and a source tree that lost its executable bits still hashes to the
// same value.
//
// The directory names are in it because a directory can be observable while
// holding no file: an empty `skills/` and an absent `skills/` take different
// branches in `installSkills` (its `existsSync`), so a stamp that could not
// tell them apart would describe two sessions that ran differently as having
// run the same commons (V5.0 audit row 32). The two section labels keep a
// directory's name from ever colliding with a file's.
function hashSnapshot(dir) {
  const digest = crypto.createHash('sha256');
  digest.update('directories\n', 'utf8');
  for (const rel of listDirectories(dir)) {
    digest.update(rel, 'utf8');
    digest.update('\n');
  }
  digest.update('files\n', 'utf8');
  for (const rel of listFiles(dir)) {
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
// and are left alone.
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
 * Take one dispatch's snapshot. Returns the paths the caller needs plus the
 * version stamp. Every dispatch gets its own directory — `mkdtemp` is what
 * guarantees two successive dispatches in one container cannot share one.
 */
function createSnapshot({ source = COMMONS_SOURCE, root = SNAPSHOT_ROOT } = {}) {
  fs.mkdirSync(root, { recursive: true });
  const dispatchDir = fs.mkdtempSync(path.join(root, 'dispatch-'));
  // `mkdtemp` has already minted the directory, so everything after it runs
  // under this guard: a copy that throws — an absent mount, ENOSPC, a dangling
  // symlink — used to leave the whole directory behind, once per attempt and so
  // three times per task at the subscriber's MAX_ATTEMPTS. subscriber.js's own
  // guarded `removeSnapshot` cannot reach it, because there is no snapshot to
  // pass it: this call never returned (V5.0 audit row 27).
  try {
    const commonsDir = path.join(dispatchDir, 'commons');
    const stateDir = path.join(dispatchDir, 'state');
    fs.cpSync(source, commonsDir, { recursive: true, dereference: true });
    fs.mkdirSync(stateDir, { recursive: true });
    const executables = markExecutables(commonsDir);
    return {
      dispatchDir,
      commonsDir,
      toolsDir: path.join(commonsDir, 'tools'),
      stateDir,
      executables,
      version: hashSnapshot(commonsDir),
    };
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

module.exports = { createSnapshot, installSkills, sessionEnv, removeSnapshot, pruneSnapshotRoot };
