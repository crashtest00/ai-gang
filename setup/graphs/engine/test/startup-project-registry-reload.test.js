'use strict';

// The reload that makes the configured project reachable, and the health
// check that proves it happened.
//
// Both the work-item service and ScrumMaster read
// services/scrummaster/config/projects.json once, when they start, and
// both are started before the step that registers the configured project
// in it. ScrumMaster has always been restarted at the end of the step
// that starts the project container; the work-item service was not, and
// on a live run that was the whole of the failure: a story was
// dispatched, the commands it produced landed on
// aigang:workitems:<project> with no consumer group reading them, no
// subtask was ever created, no agent ran — and no log anywhere carried an
// error.
//
// Both steps are driven for real, with a stand-in `docker` in place of
// the daemon: it records every command line, and reports the project's
// consumer group only once the consumers have actually been restarted,
// which is exactly what the daemon would do.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const REPO_ROOT = path.join(__dirname, '..', '..', '..', '..');
const STARTUP_DIR = path.join(REPO_ROOT, 'scripts', 'startup');
const PROJECT = 'acceptance-project';
const COMMAND_STREAM = `aigang:workitems:${PROJECT}`;

const CONFIG = {
  schemaVersion: 1,
  project: { name: PROJECT, type: 'web', stack: 'node-express' },
  repository: { url: 'https://github.com/an-org/a-repo.git' },
};

// A `docker` that answers the four questions these two steps ask —
// is the project container running, does the API answer /health, is the
// project's subscriber up, and does the project's command stream have a
// consumer group — and records every call. The consumer group appears
// when, and only when, the consumers have been restarted: without that
// restart the stand-in behaves exactly as the daemon did on the live run.
//
// The XINFO GROUPS listing is split across two writes with a real pause
// between them when `groupsReplyPauseSeconds` is given, standing in for
// redis-cli's own reply arriving in more than one read: the target group
// is in the first write, everything after it (consumers, pending, and
// more) in the second.
function stubDocker(dir, { restartCreatesGroup = true, groupsReplyPauseSeconds = 0 } = {}) {
  const bin = path.join(dir, 'bin');
  fs.mkdirSync(bin, { recursive: true });
  const stub = path.join(bin, 'docker');
  const groupsReply = groupsReplyPauseSeconds > 0
    ? [
        '        printf "name\\nworkitemservice\\n"',
        `        sleep ${groupsReplyPauseSeconds}`,
        '        printf "consumers\\n1\\npending\\n0\\n"',
      ].join('\n')
    : '        printf "name\\nworkitemservice\\nconsumers\\n1\\npending\\n0\\n"';
  fs.writeFileSync(stub, [
    '#!/usr/bin/env bash',
    'printf "%s|%s\\n" "${PWD#$AIGANG_TEST_ROOT/}" "$*" >> "$AIGANG_TEST_ARGV"',
    'if [[ "$1" == "compose" ]]; then',
    '  if [[ "$*" == *" restart "* && "$*" == *"consumers"* ]]; then',
    `    ${restartCreatesGroup ? 'printf "restarted\\n" > "$AIGANG_TEST_GROUP_FLAG"' : ':'}`,
    '  fi',
    '  exit 0',
    'fi',
    'if [[ "$1" == "inspect" ]]; then printf "true\\n"; exit 0; fi',
    'if [[ "$1" == "exec" ]]; then',
    '  shift',
    '  case "$*" in',
    '    *"XINFO GROUPS"*)',
    '      if [[ -f "$AIGANG_TEST_GROUP_FLAG" ]]; then',
    groupsReply,
    '      fi',
    '      exit 0 ;;',
    '    *"redis-cli ping"*) printf "PONG\\n"; exit 0 ;;',
    '    *"pm2 jlist"*) printf \'[{"name":"subscriber","pm2_env":{"status":"online"}}]\\n\'; exit 0 ;;',
    '  esac',
    '  exit 0',
    'fi',
    'exit 0',
    '',
  ].join('\n'));
  fs.chmodSync(stub, 0o755);
  return bin;
}

// A temporary checkout holding only what these two steps read: the
// configuration, the project directory and its Dockerfile, and the two
// service directories `docker compose` is run from.
function makeRoot() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'aigang-registry-reload-'));
  fs.writeFileSync(path.join(root, 'ai-gang.config.json'), JSON.stringify(CONFIG));
  fs.mkdirSync(path.join(root, 'projects', PROJECT), { recursive: true });
  fs.writeFileSync(path.join(root, 'projects', PROJECT, 'Dockerfile'), 'FROM node:22-alpine\n');
  fs.mkdirSync(path.join(root, 'services', 'scrummaster'), { recursive: true });
  fs.mkdirSync(path.join(root, 'services', 'work-item-service'), { recursive: true });
  // The validator the steps read their decisions back through lives in
  // the real checkout, so it is linked rather than copied.
  fs.mkdirSync(path.join(root, 'setup', 'graphs', 'engine', 'lib'), { recursive: true });
  fs.symlinkSync(
    path.join(REPO_ROOT, 'setup', 'graphs', 'engine', 'lib', 'config'),
    path.join(root, 'setup', 'graphs', 'engine', 'lib', 'config')
  );
  return root;
}

function environment(root, options = {}) {
  const stateDir = path.join(root, '.ai-gang');
  fs.mkdirSync(stateDir, { recursive: true });
  const env = {
    ...process.env,
    PATH: `${stubDocker(root, options)}:${process.env.PATH}`,
    AIGANG_ROOT: root,
    AIGANG_STATE_DIR: stateDir,
    AIGANG_TEST_ROOT: root,
    AIGANG_TEST_ARGV: path.join(root, 'argv'),
    AIGANG_TEST_GROUP_FLAG: path.join(root, 'consumer-group'),
    // A wait that is never going to succeed should fail the test, not
    // sit out the real sixty seconds first.
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
  const file = env.AIGANG_TEST_ARGV;
  if (!fs.existsSync(file)) return [];
  return fs.readFileSync(file, 'utf8').trim().split('\n').filter(Boolean)
    .map((line) => {
      const [cwd, ...rest] = line.split('|');
      return { cwd, command: rest.join('|') };
    });
}

function record(env) {
  return JSON.parse(fs.readFileSync(path.join(env.AIGANG_STATE_DIR, 'status.json'), 'utf8'));
}

function completeStepsBefore(id, env) {
  const list = spawnSync('bash', [path.join(STARTUP_DIR, 'steps.sh'), 'list'], { encoding: 'utf8', timeout: 15000 });
  assert.equal(list.status, 0, list.stderr);
  for (const line of list.stdout.trim().split('\n')) {
    const stepId = line.split('|')[1];
    if (stepId === id) continue;
    for (const action of ['step-start', 'step-done']) {
      const result = spawnSync('bash', [path.join(STARTUP_DIR, 'status.sh'), action, stepId], {
        env, encoding: 'utf8', timeout: 15000,
      });
      assert.equal(result.status, 0, result.stderr);
    }
  }
}

// ---- the reload ----

test('the step that starts the project reloads the work-item service too, and waits for it', () => {
  const root = makeRoot();
  const env = environment(root);
  const result = runStep('start-project.sh', env);
  assert.equal(result.status, 0, `${result.stdout}\n${result.stderr}`);

  const restarts = calls(env).filter((c) => c.command.startsWith('compose restart'));
  assert.deepEqual(
    restarts.map((c) => `${c.cwd}: ${c.command}`),
    [
      'services/scrummaster: compose restart scrummaster',
      'services/work-item-service: compose restart api consumers',
    ],
    'the project registry has to be reloaded in both services that read it'
  );

  // The step does not take the restart's word for it: the project's
  // consumer group is what it waits for.
  assert.ok(
    calls(env).some((c) => c.command.includes(`XINFO GROUPS ${COMMAND_STREAM}`)),
    "the step must confirm the project's command stream has a consumer group"
  );
  assert.match(result.stdout, new RegExp(`consuming commands for '${PROJECT}'`));
});

test('the outbox relay is left running — it never reads the project list', () => {
  const root = makeRoot();
  const env = environment(root);
  assert.equal(runStep('start-project.sh', env).status, 0);
  const restarted = calls(env).filter((c) => c.command.startsWith('compose restart'));
  assert.equal(
    restarted.some((c) => c.command.includes('relay')),
    false,
    'the relay takes each row’s project from the row itself; restarting it would only drop work'
  );
});

test('a work-item service that comes back still not serving the project fails the step, by name', () => {
  const root = makeRoot();
  const env = environment(root, { restartCreatesGroup: false });
  const result = runStep('start-project.sh', env);
  assert.notEqual(result.status, 0, 'a reload that did not take must not pass as a started project');
  assert.match(result.stderr, new RegExp(`not consuming '${PROJECT}'`));
  assert.ok(result.stderr.includes(COMMAND_STREAM), `the diagnostic must name ${COMMAND_STREAM}`);
  assert.equal(record(env).state, 'failed');
  assert.ok(record(env).error.includes(PROJECT));
});

// ---- the health check ----

test('health confirms the work-item service is serving the configured project', () => {
  const root = makeRoot();
  const env = environment(root);
  fs.writeFileSync(env.AIGANG_TEST_GROUP_FLAG, 'restarted\n');
  completeStepsBefore('confirm-health', env);

  const result = runStep('confirm-health.sh', env);
  assert.equal(result.status, 0, `${result.stdout}\n${result.stderr}`);
  assert.ok(
    calls(env).some((c) => c.command.includes(`XINFO GROUPS ${COMMAND_STREAM}`)),
    'the health check must ask about the configured project, not only about the containers'
  );
  const doc = record(env);
  assert.equal(doc.services['workitem-consumers'], 'healthy');
  assert.equal(doc.state, 'complete');
});

// redis-cli's real XINFO GROUPS reply arrives in more than one piece for
// any group with company (consumers, pending, more groups): the target
// group can be read before the rest of the reply has. A check that reads
// only up to its own match and stops closes the pipe out from under
// redis-cli — which then dies of SIGPIPE finishing a write nothing is
// reading — and under `set -o pipefail` that failure, not the match,
// is what the health check used to see.
test('the health check still recognizes the group once found, even while more of the reply is still arriving', () => {
  const root = makeRoot();
  const env = environment(root, { groupsReplyPauseSeconds: 0.2 });
  fs.writeFileSync(env.AIGANG_TEST_GROUP_FLAG, 'restarted\n');
  completeStepsBefore('confirm-health', env);

  const result = runStep('confirm-health.sh', env);
  assert.equal(result.status, 0, `${result.stdout}\n${result.stderr}`);
  const doc = record(env);
  assert.equal(doc.services['workitem-consumers'], 'healthy');
  assert.equal(doc.state, 'complete');
});

test('health fails, naming the project, when the consumers are not serving it', () => {
  // Every container is up and every other check passes: this is exactly
  // the state the live run ended in, and the run reported success.
  const root = makeRoot();
  const env = environment(root);
  completeStepsBefore('confirm-health', env);

  const result = runStep('confirm-health.sh', env);
  assert.notEqual(result.status, 0, 'a project nothing is consuming is not a healthy platform');
  assert.match(result.stderr, /workitem-consumers: NOT healthy/);
  assert.ok(result.stderr.includes(PROJECT), 'the diagnostic must name the project');
  assert.ok(result.stderr.includes(COMMAND_STREAM), `the diagnostic must name ${COMMAND_STREAM}`);

  const doc = record(env);
  assert.equal(doc.state, 'failed');
  assert.equal(doc.services['workitem-consumers'], 'unhealthy');
  assert.equal(doc.services.redis, 'healthy');
  assert.equal(doc.adminUrl, null, 'a failed health check must not publish an address to open');
  assert.ok(doc.error.includes(PROJECT));
});
