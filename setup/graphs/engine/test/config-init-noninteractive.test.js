'use strict';

// The three prompts scripts/init-project.sh --config still made, driven
// through the real script with no terminal at all — the way platform
// startup runs it: the repository URL, the fine-grained PAT, and the
// final "Continue? [y/N]" confirmation.
//
// The confirmation is the one that used to be silently fatal. With stdin
// closed, `read` returns EOF, the answer reads as "N", and the run aborts
// having done nothing — reported as a refusal nobody made.
//
// Isolation: every run gets its own temp projects directory, its own
// projects.json fixture, and its own HQ_ENV, through the overrides
// scripts/init-project.sh already carries. No run reaches a network: the
// only remote any test configures is a local bare repository.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync, execFileSync } = require('node:child_process');

const REPO_ROOT = path.join(__dirname, '..', '..', '..', '..');
const SCRIPT_PATH = path.join(REPO_ROOT, 'scripts', 'init-project.sh');

const BASE_CONFIG = {
  schemaVersion: 1,
  project: { name: 'acceptance-project', type: 'web', stack: 'node-express' },
};

function makeIsolatedEnv({ hqEnvLines = null } = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'aigang-noninteractive-'));
  const projectsDir = path.join(root, 'projects');
  fs.mkdirSync(projectsDir, { recursive: true });
  const projectsConfigPath = path.join(root, 'projects.json');
  fs.writeFileSync(projectsConfigPath, JSON.stringify({ projects: [] }, null, 2));

  const hqEnvPath = path.join(root, 'platform.env');
  if (hqEnvLines !== null) fs.writeFileSync(hqEnvPath, hqEnvLines);

  const env = {
    ...process.env,
    AIGANG_PROJECTS_DIR: projectsDir,
    AIGANG_PROJECTS_CONFIG: projectsConfigPath,
    HQ_ENV: hqEnvLines === null ? path.join(root, 'nonexistent.env') : hqEnvPath,
    GIT_AUTHOR_NAME: 'Test', GIT_AUTHOR_EMAIL: 'test@example.invalid',
    GIT_COMMITTER_NAME: 'Test', GIT_COMMITTER_EMAIL: 'test@example.invalid',
  };
  delete env.GH_TOKEN;
  return { root, projectsDir, projectsConfigPath, env };
}

function writeConfig(root, config) {
  const file = path.join(root, 'ai-gang.config.json');
  fs.writeFileSync(file, JSON.stringify(config));
  return file;
}

// stdin closed: no terminal, and nothing to read even if something tried.
function runWithNoTerminal(args, env) {
  return spawnSync('bash', [SCRIPT_PATH, ...args], {
    cwd: REPO_ROOT,
    env,
    input: '',
    stdio: ['pipe', 'pipe', 'pipe'],
    encoding: 'utf8',
    timeout: 60000,
  });
}

function makeBareRepo(root) {
  const bare = path.join(root, 'remote');
  execFileSync('git', ['init', '--bare', '-q', bare]);
  return `file://${bare}`;
}

test('with no terminal, a --config run completes instead of aborting at the confirmation', () => {
  const { root, projectsDir, projectsConfigPath, env } = makeIsolatedEnv();
  const configFile = writeConfig(root, BASE_CONFIG);

  const result = runWithNoTerminal(['--config', configFile], env);

  assert.equal(result.status, 0, `${result.stdout}\n${result.stderr}`);
  assert.equal(result.stdout.includes('Aborted.'), false, 'an EOF must not read as a refusal');
  assert.match(result.stdout, /no terminal — proceeding from/);
  assert.ok(fs.existsSync(path.join(projectsDir, 'acceptance-project', 'src')));
  const registered = JSON.parse(fs.readFileSync(projectsConfigPath, 'utf8'));
  assert.deepEqual(registered.projects.map((p) => p.name), ['acceptance-project']);
});

test('the repository URL comes from the configuration, with no prompt', () => {
  const { root, projectsDir, env } = makeIsolatedEnv();
  const remote = makeBareRepo(root);
  const configFile = writeConfig(root, { ...BASE_CONFIG, repository: { url: remote } });

  const result = runWithNoTerminal(['--config', configFile], env);
  assert.equal(result.status, 0, `${result.stdout}\n${result.stderr}`);
  assert.match(result.stdout, new RegExp(`GitHub repository \\(from .*\\): ${remote.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}`));
  assert.equal(result.stdout.includes('paste the HTTPS URL below'), false, 'the URL prompt must not appear');

  const configured = execFileSync('git', ['-C', path.join(projectsDir, 'acceptance-project', 'src'), 'remote', 'get-url', 'origin'], { encoding: 'utf8' });
  assert.equal(configured.trim(), remote);
});

test('the PAT comes from the environment file, with no prompt, and is never echoed', () => {
  const secret = 'github_pat_test_not_a_real_token';
  const { root, env } = makeIsolatedEnv({ hqEnvLines: `GH_TOKEN=${secret}\n` });
  const remote = makeBareRepo(root);
  const configFile = writeConfig(root, { ...BASE_CONFIG, repository: { url: remote } });

  const result = runWithNoTerminal(['--config', configFile], env);
  assert.equal(result.status, 0, `${result.stdout}\n${result.stderr}`);
  assert.match(result.stdout, /GitHub PAT: read from/);
  assert.equal(result.stdout.includes('Generate one at'), false, 'the PAT prompt must not appear');
  assert.equal(`${result.stdout}${result.stderr}`.includes(secret), false, 'the PAT must never be echoed');

  // With a PAT and a remote, the first commit is pushed.
  const branches = execFileSync('git', ['-C', root, 'ls-remote', '--heads', remote], { encoding: 'utf8' });
  assert.match(branches, /refs\/heads\/(main|master)/);
});

test('no configured URL and no terminal skips the remote rather than blocking', () => {
  const { root, projectsDir, env } = makeIsolatedEnv();
  const configFile = writeConfig(root, BASE_CONFIG);

  const result = runWithNoTerminal(['--config', configFile], env);
  assert.equal(result.status, 0, `${result.stdout}\n${result.stderr}`);
  assert.match(result.stdout, /no terminal to ask — skipping the remote/);

  const remotes = execFileSync('git', ['-C', path.join(projectsDir, 'acceptance-project', 'src'), 'remote'], { encoding: 'utf8' });
  assert.equal(remotes.trim(), '');
});

test('a second no-terminal run with the same configuration resumes and registers nothing twice', () => {
  const { root, projectsConfigPath, env } = makeIsolatedEnv();
  const configFile = writeConfig(root, BASE_CONFIG);

  assert.equal(runWithNoTerminal(['--config', configFile], env).status, 0);
  const second = runWithNoTerminal(['--config', configFile], env);
  assert.equal(second.status, 0, `${second.stdout}\n${second.stderr}`);
  assert.match(second.stdout, /resuming idempotently/);

  const registered = JSON.parse(fs.readFileSync(projectsConfigPath, 'utf8'));
  assert.equal(registered.projects.length, 1);
});

test('a changed project name initializes a second project rather than being refused', () => {
  // Not a defect in this script: its configuration-identity record is
  // keyed by the project directory, so a renamed project addresses a
  // different directory and is a new project by construction. This is
  // exactly the gap the platform's own checkout-level record closes —
  // see scripts/startup/config-identity.sh and its test.
  const { root, projectsDir, projectsConfigPath, env } = makeIsolatedEnv();
  const configFile = writeConfig(root, BASE_CONFIG);
  assert.equal(runWithNoTerminal(['--config', configFile], env).status, 0);

  fs.writeFileSync(configFile, JSON.stringify({
    ...BASE_CONFIG,
    project: { ...BASE_CONFIG.project, name: 'a-renamed-project' },
  }));
  const renamed = runWithNoTerminal(['--config', configFile], env);
  assert.equal(renamed.status, 0, `${renamed.stdout}\n${renamed.stderr}`);

  assert.deepEqual(fs.readdirSync(projectsDir).sort(), ['a-renamed-project', 'acceptance-project']);
  const registered = JSON.parse(fs.readFileSync(projectsConfigPath, 'utf8'));
  assert.equal(registered.projects.length, 2);
});

test('the same project directory with changed decisions is refused before further change', () => {
  const { root, env } = makeIsolatedEnv();
  const configFile = writeConfig(root, BASE_CONFIG);
  assert.equal(runWithNoTerminal(['--config', configFile], env).status, 0);

  // Rewrite the project's own identity record as if it had been
  // initialized for a different target, then re-run the same
  // configuration against it.
  const identity = path.join(env.AIGANG_PROJECTS_DIR, 'acceptance-project', '.aigang-config-identity.json');
  fs.writeFileSync(identity, JSON.stringify({
    schemaVersion: 1, name: 'acceptance-project', type: 'desktop', stack: 'node-express',
  }));

  const result = runWithNoTerminal(['--config', configFile], env);
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /already initialised with a different configuration/);
});

test('an invalid configuration still fails before anything is created, with no terminal', () => {
  const { root, projectsDir, projectsConfigPath, env } = makeIsolatedEnv();
  const configFile = writeConfig(root, {
    ...BASE_CONFIG,
    project: { name: 'acceptance-project', type: 'web', stack: 'not-a-stack' },
  });

  const result = runWithNoTerminal(['--config', configFile], env);
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /not a supported stack profile/);
  assert.deepEqual(fs.readdirSync(projectsDir), []);
  assert.deepEqual(JSON.parse(fs.readFileSync(projectsConfigPath, 'utf8')).projects, []);
});

test('without --config the confirmation still has to be answered', () => {
  // The interactive flow is unchanged: answering anything but y aborts.
  const { root, projectsDir, env } = makeIsolatedEnv();
  const result = spawnSync('bash', [SCRIPT_PATH], {
    cwd: REPO_ROOT,
    env,
    input: 'web\ninteractive-project\n\nn\n',
    encoding: 'utf8',
    timeout: 60000,
  });
  assert.equal(result.status, 0);
  assert.match(result.stdout, /Aborted\./);
  assert.deepEqual(fs.readdirSync(projectsDir), []);
});
