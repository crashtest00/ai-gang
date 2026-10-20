'use strict';

// Memory's indexer starts at step 9, not step 6. Its first pass embeds the
// platform corpus, which is CPU-heavy, and on a small host it starved the
// project container's build. Step 6 therefore starts every other Memory
// service and waits for those; step 9 starts the indexer once the project
// container is up; step 10 still requires all five containers.
//
// docker is a stand-in that records every call and answers the questions
// these steps ask; nothing here touches a real daemon.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const REPO_ROOT = path.join(__dirname, '..', '..', '..', '..');
const STARTUP_DIR = path.join(REPO_ROOT, 'scripts', 'startup');
const PROJECT = 'indexer-project';
const NOT_INDEXER = ['memory-copier', 'memory-api', 'memory-extraction', 'memory-extractor'];
const COMPOSE_SERVICES = [
  'memory-postgres', 'migrate', 'memory-copier', 'memory-api',
  'memory-extraction', 'memory-extractor', 'memory-indexer',
];

const CONFIG = {
  schemaVersion: 2,
  authMethod: 'api-key',
  project: { name: PROJECT, type: 'web', stack: 'node-express' },
  repository: { url: 'https://github.com/an-org/a-repo.git' },
};

// `stopped` names containers that report not running. Everything else is
// running. The core consumer group exists from the start so step 9's
// existing wait passes.
function stubDocker(dir, stopped) {
  const bin = path.join(dir, 'bin');
  fs.mkdirSync(bin, { recursive: true });
  const stub = path.join(bin, 'docker');
  fs.writeFileSync(stub, [
    '#!/usr/bin/env bash',
    'printf "%s|%s\\n" "${PWD#$AIGANG_TEST_ROOT/}" "$*" >> "$AIGANG_TEST_ARGV"',
    'if [[ "$1" == "inspect" ]]; then',
    ...stopped.map((name) => `  if [[ "$*" == *" ${name}"* ]]; then printf "false\\n"; exit 0; fi`),
    '  printf "true\\n"; exit 0',
    'fi',
    'if [[ "$1" == "exec" && "$*" == *"XINFO GROUPS"* ]]; then printf "name\\ncore\\n"; fi',
    'exit 0',
    '',
  ].join('\n'));
  fs.chmodSync(stub, 0o755);
  return bin;
}

function makeEnv({ stopped = [] } = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'aigang-indexer-deferral-'));
  fs.writeFileSync(path.join(root, 'ai-gang.config.json'), JSON.stringify(CONFIG));
  fs.mkdirSync(path.join(root, 'projects', PROJECT), { recursive: true });
  fs.writeFileSync(path.join(root, 'projects', PROJECT, 'Dockerfile'), 'FROM node:22-alpine\n');
  for (const service of ['scrummaster', 'core', 'memory']) {
    fs.mkdirSync(path.join(root, 'services', service), { recursive: true });
  }
  fs.writeFileSync(path.join(root, 'services', 'memory', '.env'), 'X=1\n');
  fs.mkdirSync(path.join(root, 'setup', 'graphs', 'engine', 'lib'), { recursive: true });
  fs.symlinkSync(
    path.join(REPO_ROOT, 'setup', 'graphs', 'engine', 'lib', 'config'),
    path.join(root, 'setup', 'graphs', 'engine', 'lib', 'config')
  );
  const stateDir = path.join(root, '.ai-gang');
  fs.mkdirSync(stateDir, { recursive: true });
  const env = {
    ...process.env,
    PATH: `${stubDocker(root, stopped)}:${process.env.PATH}`,
    AIGANG_ROOT: root,
    AIGANG_STATE_DIR: stateDir,
    AIGANG_TEST_ROOT: root,
    AIGANG_TEST_ARGV: path.join(root, 'argv'),
    AIGANG_WAIT_ATTEMPTS: '3',
    AIGANG_WAIT_INTERVAL: '0',
  };
  const init = spawnSync('bash', [path.join(STARTUP_DIR, 'status.sh'), 'init'], {
    env, encoding: 'utf8', timeout: 15000,
  });
  assert.equal(init.status, 0, init.stderr);
  return env;
}

function runStep(script, env) {
  return spawnSync('bash', [path.join(STARTUP_DIR, script)], { env, encoding: 'utf8', timeout: 60000 });
}

function calls(env) {
  if (!fs.existsSync(env.AIGANG_TEST_ARGV)) return [];
  return fs.readFileSync(env.AIGANG_TEST_ARGV, 'utf8').trim().split('\n').filter(Boolean)
    .map((line) => {
      const [cwd, ...rest] = line.split('|');
      return { cwd, command: rest.join('|') };
    });
}

function memoryUps(env) {
  return calls(env).filter((c) => c.cwd === 'services/memory' && c.command.startsWith('compose up'));
}

// ---- step 6 ----

test('step 6 names every Memory service except the indexer, and logs that the indexer is held', () => {
  const env = makeEnv();
  const result = runStep('start-memory.sh', env);
  assert.equal(result.status, 0, `${result.stdout}\n${result.stderr}`);

  const ups = memoryUps(env);
  assert.equal(ups.length, 1, 'step 6 brings Memory up with one compose call');
  const named = ups[0].command.split(/\s+/).filter((word) => COMPOSE_SERVICES.includes(word));
  assert.deepEqual(
    named.sort(),
    COMPOSE_SERVICES.filter((s) => s !== 'memory-indexer').sort(),
    'every service but memory-indexer, named, so nothing starts it as a dependency'
  );
  assert.ok(!ups[0].command.includes('memory-indexer'));
  assert.match(result.stdout, /memory-indexer is held until step 9/);
});

test('step 6 does not wait for the indexer', () => {
  const env = makeEnv({ stopped: ['memory-indexer'] });
  const result = runStep('start-memory.sh', env);
  assert.equal(result.status, 0, `${result.stdout}\n${result.stderr}`);
  assert.ok(
    !calls(env).some((c) => c.command.startsWith('inspect') && c.command.includes('memory-indexer')),
    'step 6 must not ask about the indexer'
  );
});

// ---- the check in lib.sh ----

function memoryRunning(env, ...args) {
  return spawnSync('bash', ['-c', `source "${path.join(STARTUP_DIR, 'lib.sh')}"; memory_running ${args.join(' ')}`], {
    env, encoding: 'utf8', timeout: 15000,
  }).status;
}

test('memory_running --without-indexer ignores the indexer; plain memory_running requires it', () => {
  const env = makeEnv({ stopped: ['memory-indexer'] });
  assert.equal(memoryRunning(env, '--without-indexer'), 0);
  assert.notEqual(memoryRunning(env), 0);
});

for (const stopped of NOT_INDEXER) {
  test(`memory_running --without-indexer still fails when ${stopped} is stopped`, () => {
    const env = makeEnv({ stopped: [stopped] });
    assert.notEqual(memoryRunning(env, '--without-indexer'), 0);
  });
}

// ---- step 9 ----

test('step 9 starts the indexer after the project container and the reloads', () => {
  const env = makeEnv();
  const result = runStep('start-project.sh', env);
  assert.equal(result.status, 0, `${result.stdout}\n${result.stderr}`);

  const all = calls(env);
  const projectUp = all.findIndex((c) => c.cwd === `projects/${PROJECT}` && c.command.startsWith('compose up'));
  const coreRestart = all.findIndex((c) => c.cwd === 'services/core' && c.command.startsWith('compose restart'));
  const indexerUp = all.findIndex((c) => c.cwd === 'services/memory' && c.command === 'compose up -d memory-indexer');
  assert.ok(projectUp >= 0 && coreRestart >= 0 && indexerUp >= 0, JSON.stringify(all));
  assert.ok(projectUp < coreRestart && coreRestart < indexerUp,
    'the indexer starts last: after the project container is up and the reloads are done');
  assert.match(result.stdout, /memory-indexer is running/);
});

test('step 9 fails, by name, when the indexer does not come up', () => {
  const env = makeEnv({ stopped: ['memory-indexer'] });
  const result = runStep('start-project.sh', env);
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /memory-indexer is not running/);
  assert.equal(JSON.parse(fs.readFileSync(path.join(env.AIGANG_STATE_DIR, 'status.json'), 'utf8')).state, 'failed');
});

// ---- the contract ----

test('steps.sh says where the indexer starts, in step 6 and step 9', () => {
  const list = spawnSync('bash', [path.join(STARTUP_DIR, 'steps.sh'), 'list'], { encoding: 'utf8', timeout: 15000 });
  assert.equal(list.status, 0, list.stderr);
  const byId = Object.fromEntries(list.stdout.trim().split('\n').map((l) => {
    const [, id, , description] = l.split('|');
    return [id, description];
  }));
  assert.match(byId['start-memory'], /without its indexer/);
  assert.match(byId['start-project'], /then start Memory's indexer/);
});
