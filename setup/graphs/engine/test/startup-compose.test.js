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

test('a second `docker compose up` rebuilds the image, so a fix to the entrypoint reaches it', () => {
  // The entrypoint is the only file baked in; every step script comes
  // from the mounted checkout and is therefore always current. An
  // operator who pulls a fix to the entrypoint and runs the documented
  // command again would otherwise silently run the old one.
  const service = compose().services['ai-gang'];
  assert.equal(service.image, undefined, 'a fixed image tag lets Compose reuse an image built elsewhere');
  assert.equal(service.pull_policy, 'build');
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

test('the image ships no world-writable home', () => {
  // The entrypoint chowns this directory to the checkout's owner at run
  // time, while it is still root, so the image only has to ship it owned
  // by somebody — not writable by everybody in every container built
  // from it.
  const text = dockerfile();
  assert.equal(/chmod\s+0?777\s+\/home\/aigang/.test(text), false, 'the home must not be world-writable');
  assert.match(text, /chmod 0755 \/home\/aigang/);
  assert.match(text, /chown \d+:\d+ \/home\/aigang/);
});

test("the image names the project's first branch, so it is not `master`", () => {
  // init-project.sh runs `git init` in the new project and cuts
  // dev/beta/prod from whatever it produced. Unset, git's built-in
  // default makes that `master`.
  assert.match(dockerfile(), /git config --system init\.defaultBranch main/);
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

// ---- Memory's compose file takes its database credential from Memory's .env alone ----
// Platform startup runs with the core service's PGUSER/PGPASSWORD/PGDATABASE
// in its environment, and Compose interpolates the calling environment before
// any .env file. memory-postgres must therefore carry no ${...} reference for
// its credential, or its database is initialised with Core's password while
// migrate/memory-copier/memory-api/memory-extraction/memory-indexer use
// Memory's own (REQ-01, REQ-02).

const MEMORY_COMPOSE_PATH = path.join(REPO_ROOT, 'services', 'memory', 'docker-compose.yml');

test("memory-postgres's credential does not come from host interpolation (static)", () => {
  const compose = yaml.load(fs.readFileSync(MEMORY_COMPOSE_PATH, 'utf8'));
  const service = compose.services['memory-postgres'];
  const interpolated = JSON.stringify([service.environment || [], service.healthcheck || {}]);
  assert.doesNotMatch(
    interpolated,
    /(?<!\$)\$\{?(PG|POSTGRES)[A-Z_]*/,
    'memory-postgres references PG*/POSTGRES_* by ${...} interpolation, which the calling environment overrides'
  );
  assert.equal(service.env_file, '.env');
});

test("resolved memory-postgres credentials are Memory's .env values even when the caller's environment holds Core's", () => {
  const { execFileSync } = require('node:child_process');
  const os = require('node:os');
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'memory-compose-'));
  try {
    fs.copyFileSync(MEMORY_COMPOSE_PATH, path.join(dir, 'docker-compose.yml'));
    fs.writeFileSync(
      path.join(dir, '.env'),
      [
        'PGUSER=memory',
        'PGPASSWORD=memory-own-password',
        'PGDATABASE=memory',
        'POSTGRES_USER=memory',
        'POSTGRES_PASSWORD=memory-own-password',
        'POSTGRES_DB=memory',
        '',
      ].join('\n')
    );
    const out = execFileSync('docker', ['compose', '-f', path.join(dir, 'docker-compose.yml'), '--env-file', path.join(dir, '.env'), 'config', '--format', 'json'], {
      env: { ...process.env, PGUSER: 'core', PGPASSWORD: 'core-password', PGDATABASE: 'core' },
      encoding: 'utf8',
    });
    const service = JSON.parse(out).services['memory-postgres'];
    assert.equal(service.environment.POSTGRES_USER, 'memory');
    assert.equal(service.environment.POSTGRES_PASSWORD, 'memory-own-password');
    assert.equal(service.environment.POSTGRES_DB, 'memory');
    assert.doesNotMatch(JSON.stringify(service), /core-password/);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
