'use strict';

// The root docker-compose.yml and Dockerfile — what `docker compose up`
// at the checkout root actually declares.
//
// These are structural checks on the shipped files. They are not a
// substitute for the live run: only a real `docker compose up` on a bare
// host proves one container starts and its initialization brings the rest
// up as siblings. What they do prove is that the declarations a live run
// depends on are present and have not been quietly changed — one service,
// no restart policy, the daemon's socket, and the checkout mounted at its
// own path with the working directory to match.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const yaml = require('js-yaml');

const REPO_ROOT = path.join(__dirname, '..', '..', '..', '..');
const COMPOSE_PATH = path.join(REPO_ROOT, 'docker-compose.yml');
const DOCKERFILE_PATH = path.join(REPO_ROOT, 'Dockerfile');

function compose() {
  return yaml.load(fs.readFileSync(COMPOSE_PATH, 'utf8'));
}

function dockerfile() {
  return fs.readFileSync(DOCKERFILE_PATH, 'utf8');
}

test('the root compose file declares exactly one service', () => {
  const names = Object.keys(compose().services);
  assert.deepEqual(names, ['ai-gang']);
});

test('it includes or extends no other compose file', () => {
  const doc = compose();
  assert.equal(doc.include, undefined, 'the per-service compose files are invoked by path, not included');
  assert.equal(doc.services['ai-gang'].extends, undefined);
});

test('the one container does not restart itself', () => {
  // Initialization runs once. A restart policy would re-run it on every
  // daemon start, and `docker compose up` would never return.
  assert.equal(compose().services['ai-gang'].restart, 'no');
});

test("it mounts the daemon's socket, so the container is a client and not a second daemon", () => {
  const volumes = compose().services['ai-gang'].volumes;
  assert.ok(volumes.includes('/var/run/docker.sock:/var/run/docker.sock'));
});

test('it mounts the checkout at the checkout\'s own path, with the working directory to match', () => {
  // A service compose file's ../scrummaster/config or ../../setup is
  // resolved by the daemon on the host. Mounted anywhere else, every
  // relative bind mount would resolve to a path the daemon cannot find
  // and would silently create as an empty directory.
  const service = compose().services['ai-gang'];
  assert.ok(service.volumes.includes('${PWD}:${PWD}'));
  assert.match(service.working_dir, /^\$\{PWD(:\?[^}]*)?\}$/);
});

test('it fails loudly rather than silently if the working directory is unknown', () => {
  assert.match(compose().services['ai-gang'].working_dir, /^\$\{PWD:\?/);
});

test('it reads the operator\'s .env', () => {
  assert.equal(compose().services['ai-gang'].env_file, '.env');
});

test('it declares no privileged mode and no extra capabilities', () => {
  const service = compose().services['ai-gang'];
  assert.equal(service.privileged, undefined);
  assert.equal(service.cap_add, undefined);
  assert.equal(service.pid, undefined);
});

test('the image carries Claude Code, the Docker CLI with Compose, git, gh and jq', () => {
  const text = dockerfile();
  assert.match(text, /@anthropic-ai\/claude-code/);
  assert.match(text, /docker-ce-cli/);
  assert.match(text, /docker-compose-plugin/);
  assert.match(text, /\bgh\b/);
  assert.match(text, /\bgit\b/);
  assert.match(text, /\bjq\b/);
});

test('the image installs no Docker daemon', () => {
  const text = dockerfile();
  assert.equal(/docker-ce\b(?!-cli)/.test(text), false, 'docker-ce would be a daemon, not a client');
  assert.equal(/dind/.test(text), false);
});

test('the entrypoint baked into the image is the startup entrypoint', () => {
  const text = dockerfile();
  assert.match(text, /COPY scripts\/startup\/entrypoint\.sh/);
  assert.match(text, /ENTRYPOINT \["\/opt\/ai-gang\/entrypoint\.sh"\]/);
  const entrypoint = path.join(REPO_ROOT, 'scripts', 'startup', 'entrypoint.sh');
  assert.ok(fs.existsSync(entrypoint));
  assert.ok(fs.statSync(entrypoint).mode & 0o111, 'the entrypoint must be executable');
});

test('the build context is narrowed to what the image actually copies', () => {
  const ignore = fs.readFileSync(path.join(REPO_ROOT, '.dockerignore'), 'utf8');
  assert.match(ignore, /^\*$/m);
  assert.match(ignore, /^!scripts\/startup\/entrypoint\.sh$/m);
});

test("the operator's configuration copy and the startup records are not committed", () => {
  const ignore = fs.readFileSync(path.join(REPO_ROOT, '.gitignore'), 'utf8');
  assert.match(ignore, /^ai-gang\.config\.json$/m);
  assert.match(ignore, /^\.ai-gang\/$/m);
  assert.ok(fs.existsSync(path.join(REPO_ROOT, 'ai-gang.config.template.json')), 'the template itself is committed');
});

test('the image carries a git identity, without which the first project commit fails', () => {
  // scripts/init-project.sh makes the project's initial commit before it
  // pushes, and git refuses to commit with no identity configured. A
  // fresh container has none unless the image sets one. Found on a live
  // run, where initialization failed here before the remote was even
  // contacted.
  const text = dockerfile();
  assert.match(text, /git config --system user\.name/);
  assert.match(text, /git config --system user\.email/);
});
