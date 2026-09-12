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

// A `git` that records its own command line the way the host's process
// table would show it — literally /proc/$$/cmdline, the same technique
// startup-admin-account.test.js uses for the admin password — then
// delegates to the real git so the push actually happens. Every
// invocation is appended to AIGANG_TEST_GIT_ARGV, one argument per line
// with a "---" separator between calls.
function stubGit(dir) {
  const bin = path.join(dir, 'bin');
  fs.mkdirSync(bin, { recursive: true });
  const realGit = execFileSync('sh', ['-c', 'command -v git'], { encoding: 'utf8' }).trim();
  const stub = path.join(bin, 'git');
  fs.writeFileSync(stub, [
    '#!/usr/bin/env bash',
    'tr "\\0" "\\n" < /proc/$$/cmdline >> "$AIGANG_TEST_GIT_ARGV"',
    'echo "---" >> "$AIGANG_TEST_GIT_ARGV"',
    `exec "${realGit}" "$@"`,
    '',
  ].join('\n'));
  fs.chmodSync(stub, 0o755);
  return bin;
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

test('the PAT never appears on a git command line — checked from the process table\'s own view', () => {
  // The prompt's own output is not the only place a secret can leak.
  // `git -c credential.helper=...password=<token>...` puts the token in
  // an argument, which sits in the host's process table, readable by any
  // local user, for as long as that git call runs — the same exposure
  // create-admin.sh's `--env-file` fix closed for the admin password. This
  // drives the real script with a stand-in `git` that records exactly
  // what a `ps` on the host would have shown for every git call it makes,
  // then actually performs the push so the flow's behaviour is unchanged.
  const secret = 'github_pat_test_not_a_real_token';
  const { root, env } = makeIsolatedEnv({ hqEnvLines: `GH_TOKEN=${secret}\n` });
  const remote = makeBareRepo(root);
  const configFile = writeConfig(root, { ...BASE_CONFIG, repository: { url: remote } });

  const argvLog = path.join(root, 'git-argv.log');
  fs.writeFileSync(argvLog, '');
  const stubBin = stubGit(root);
  const testEnv = { ...env, PATH: `${stubBin}:${env.PATH}`, AIGANG_TEST_GIT_ARGV: argvLog };

  const result = runWithNoTerminal(['--config', configFile], testEnv);
  assert.equal(result.status, 0, `${result.stdout}\n${result.stderr}`);

  // The push still happened — the fix must not change what the flow does.
  const branches = execFileSync('git', ['-C', root, 'ls-remote', '--heads', remote], { encoding: 'utf8' });
  assert.match(branches, /refs\/heads\/(main|master)/);

  const argvText = fs.readFileSync(argvLog, 'utf8');
  assert.ok(argvText.includes('push'), `expected at least one push invocation to be recorded:\n${argvText}`);
  assert.equal(argvText.includes(secret), false, `the PAT appeared on a git command line:\n${argvText}`);
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

// ---- the environment file is data, not a script ----

test('a value in the environment file that looks like a command is a value', () => {
  // The platform .env holds a password somebody invented. Executing the
  // file — which is what sourcing it does — would expand a $, a backtick
  // or a $(...) in any of these values, and run it.
  const { root, projectsDir, env } = makeIsolatedEnv({ hqEnvLines: '' });
  const canary = path.join(root, 'executed');
  const backtickCanary = path.join(root, 'also-executed');
  const key = `sk-$(touch ${canary})-\`touch ${backtickCanary}\`-not-a-real-key`;
  fs.writeFileSync(env.HQ_ENV, [
    'GH_TOKEN=github_pat_test_not_a_real_token',
    `ANTHROPIC_API_KEY=${key}`,
    `AIGANG_ADMIN_PASSWORD=$(touch ${canary})`,
    '',
  ].join('\n'));

  const remote = makeBareRepo(root);
  const configFile = writeConfig(root, { ...BASE_CONFIG, repository: { url: remote } });
  const result = runWithNoTerminal(['--config', configFile], env);

  assert.equal(result.status, 0, `${result.stdout}\n${result.stderr}`);
  assert.equal(fs.existsSync(canary), false, 'a $(...) in an environment value must not run');
  assert.equal(fs.existsSync(backtickCanary), false, 'a backtick in an environment value must not run');

  // ...and the value still arrived, byte for byte.
  const projectEnv = fs.readFileSync(path.join(projectsDir, 'acceptance-project', '.env'), 'utf8');
  assert.ok(projectEnv.includes(`ANTHROPIC_API_KEY=${key}`), `the key was not carried over literally:\n${projectEnv}`);
});

// ---- the project's own environment file holds two secrets ----

test("the project's .env is readable only by its owner", () => {
  const { root, projectsDir, env } = makeIsolatedEnv({ hqEnvLines: 'GH_TOKEN=github_pat_test_not_a_real_token\nANTHROPIC_API_KEY=sk-not-a-real-key\n' });
  const remote = makeBareRepo(root);
  const configFile = writeConfig(root, { ...BASE_CONFIG, repository: { url: remote } });
  assert.equal(runWithNoTerminal(['--config', configFile], env).status, 0);

  const projectEnv = path.join(projectsDir, 'acceptance-project', '.env');
  const mode = fs.statSync(projectEnv).mode & 0o777;
  assert.equal(mode.toString(8), '600', 'the file holds the PAT and the Anthropic key');
});

// ---- what the run tells its caller to do next ----

test('a --config run is not told to add the Dockerfile its caller installs', () => {
  // Under --config the caller installs the project container's
  // Dockerfile from the stack's template, straight after this returns.
  const { root, env } = makeIsolatedEnv();
  const configFile = writeConfig(root, BASE_CONFIG);
  const result = runWithNoTerminal(['--config', configFile], env);

  assert.equal(result.status, 0, `${result.stdout}\n${result.stderr}`);
  assert.equal(result.stdout.includes('Add a Dockerfile'), false, result.stdout);
  assert.match(result.stdout, /Next steps:/);
  assert.match(result.stdout, /2\. docker compose build/);
});

test('the interactive path still says to add the Dockerfile, because nothing else does', () => {
  const { root, env } = makeIsolatedEnv();
  const result = spawnSync('bash', [SCRIPT_PATH], {
    cwd: REPO_ROOT,
    env,
    input: 'web\ninteractive-project\n\ny\n',
    encoding: 'utf8',
    timeout: 120000,
  });
  assert.equal(result.status, 0, `${result.stdout}\n${result.stderr}`);
  assert.match(result.stdout, /2\. Add a Dockerfile/);
  assert.match(result.stdout, /3\. docker compose build/);
});

// ---- the flag's own documentation against the flag ----

test('the --config usage comment describes every prompt --config replaces', () => {
  // Gate: the block that documents the flag is where an operator reads
  // what it does, and it went out of date as soon as the flag grew.
  const source = fs.readFileSync(SCRIPT_PATH, 'utf8');
  const start = source.indexOf('# --config <file>:');
  assert.ok(start > 0, 'the --config usage comment must exist');
  // Flattened, so a claim that wraps across two comment lines still reads as one.
  const comment = source.slice(start, source.indexOf('set -euo pipefail')).replace(/\n#\s*/g, ' ');

  // Each documented behaviour, and the code path that performs it.
  const claims = [
    [/"repository" object/, /GITHUB_URL="\$CONFIG_REPOSITORY_URL"/, 'the repository URL from the configuration'],
    [/GH_TOKEN in the environment file/, /GH_TOKEN="\$\{GH_TOKEN:-\}"/, 'the PAT from the environment'],
    [/\$HQ_ENV, read as data — never executed/, /read_env_value/, 'the environment file read without executing it'],
    [/"Continue\? \[y\/N\]" confirmation is suppressed/, /no terminal — proceeding from/, 'the suppressed confirmation'],
    [/leaves out the steps --config's caller performs itself/, /if \[\[ -z "\$CONFIG_FILE" \]\]; then\n\s+echo "  \$next_step\. Add a Dockerfile/, 'the conditional next-steps list'],
  ];
  for (const [documented, implemented, what] of claims) {
    assert.match(comment, documented, `the usage comment does not describe ${what}`);
    assert.match(source, implemented, `the usage comment describes ${what}, which the script does not do`);
  }
});

test('init-project.sh never sources $HQ_ENV — the whole script, not just the usage comment', () => {
  // An environment file is data: executing it would expand a $, a
  // backtick or a $(...) in any value, and one of those values is a
  // password somebody invented (read_env_value above). The usage comment
  // test only covers the block above `set -euo pipefail`; this guards the
  // property everywhere else in the script too, including a comment that
  // could describe the file as sourced without the code actually doing it.
  const source = fs.readFileSync(SCRIPT_PATH, 'utf8');
  assert.doesNotMatch(source, /\bsource\s+"?\$HQ_ENV"?/, 'an environment file must be read as data, never executed');
  assert.doesNotMatch(source, /sourced above/, 'nothing in this script sources $HQ_ENV, so nothing should claim it does');
});
