'use strict';

/**
 * REQ-03's Jenkins half — a `pipeline_retry` construction failure reaches the
 * build's status without aborting the pipeline-failure handler's per-ticket
 * loop (V5.0 Deterministic Gateway Message Tooling REQ-03).
 *
 * The mechanism is split in two, because a shell loop cannot set a Groovy build
 * result: the loop records each ticket whose construction failed in a marker
 * file and carries on, and the enclosing `script` block marks the build
 * UNSTABLE when the marker exists. This file proves both halves as far as they
 * can be proved without a Jenkins instance:
 *
 *  - the `script` block's half is asserted against the template's own text,
 *    which is all a Groovy build result can be checked against here;
 *  - the loop's half is *executed*. The `sh` script is extracted from
 *    setup/Jenkinsfile.template verbatim and run under `sh -xe`, the way
 *    Jenkins runs it, with only two substitutions: the `/agent-docs` bind mount
 *    becomes a temporary directory, and `curl` becomes a stub, since no Jira is
 *    reachable from a test.
 *
 * Run A uses the real publish tool, whose refusal path never opens a Redis
 * connection, so the real field-naming message and the real non-zero exit are
 * what the loop sees. Runs B and C stub the tool, because a *successful*
 * publish needs a Redis this suite has none of; the tool's own accept/refuse
 * behaviour against real Redis is covered by
 * setup/commons/tools/a2a-validate.test.js.
 *
 * What remains unproved without a Jenkins instance: that Jenkins' own
 * `fileExists`/`readFile`/`currentBuild.result` steps behave as written, and
 * that a build ends UNSTABLE rather than green or red.
 */

const { test, after } = require('node:test');
const assert = require('node:assert/strict');
const { execFileSync, spawnSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const TEMPLATE_PATH = path.join(__dirname, '..', '..', 'setup', 'Jenkinsfile.template');
const TOOLS_DIR = path.join(__dirname, '..', '..', 'setup', 'commons', 'tools');
const TEMPLATE = fs.readFileSync(TEMPLATE_PATH, 'utf8');

// The `post { failure { ... } }` handler's shell script is the last `sh '''`
// block in the template. Groovy has already un-escaped `\\` by the time the
// shell sees it, so this does the same.
function postFailureShellScript() {
  const open = TEMPLATE.lastIndexOf("sh '''");
  assert.notEqual(open, -1, 'the template must still have an sh block in its failure handler');
  const bodyStart = open + "sh '''".length;
  const end = TEMPLATE.indexOf("'''", bodyStart);
  assert.notEqual(end, -1);
  return TEMPLATE.slice(bodyStart, end).replace(/\\\\/g, '\\');
}

// Everything the template says about the marker, and what the script block
// does with it.
const POST_BLOCK = TEMPLATE.slice(TEMPLATE.indexOf('post {'));

// The same block with its comment lines dropped, so an assertion about what the
// handler *does* is not answered by a comment saying it does not.
const POST_BLOCK_CODE = POST_BLOCK.split('\n').filter(line => !line.trim().startsWith('//')).join('\n');

function redisModulePath() {
  const candidates = [
    path.join(__dirname, '..', '..', 'services', 'scrummaster', 'node_modules'),
  ];
  try {
    candidates.push(execFileSync('npm', ['root', '-g'], { encoding: 'utf8' }).trim());
  } catch { /* npm is not required for the rest of this file */ }
  const found = candidates.find(dir => dir && fs.existsSync(path.join(dir, 'redis')));
  assert.ok(found, 'the `redis` package must be resolvable to run the real publish tool');
  return found;
}

const runDirs = [];
after(() => {
  for (const dir of runDirs) fs.rmSync(dir, { recursive: true, force: true });
});

/**
 * Lay out one run: a stand-in for the `/agent-docs` bind mount, a stub `curl`
 * on PATH, and the extracted script with `/agent-docs` repointed.
 */
function prepareRun({ realTool, stubExitsFor = [] }) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'pipeline-retry-'));
  runDirs.push(dir);
  const agentDocs = path.join(dir, 'agent-docs');
  const toolsDir = path.join(agentDocs, 'commons', 'tools');
  fs.mkdirSync(toolsDir, { recursive: true });

  if (realTool) {
    fs.cpSync(TOOLS_DIR, toolsDir, { recursive: true, filter: src => !src.endsWith('.test.js') });
  } else {
    // A stand-in for the durable write only: it refuses the tickets named in
    // `stubExitsFor` the way the real tool refuses a malformed payload — a
    // field-naming message on stderr and a non-zero exit — and records the rest
    // as published.
    fs.writeFileSync(path.join(toolsDir, 'gateway-publish.js'), [
      '#!/usr/bin/env node',
      "const fs = require('fs');",
      "const payload = JSON.parse(fs.readFileSync(0, 'utf8'));",
      `const refuse = ${JSON.stringify(stubExitsFor)};`,
      'if (refuse.includes(payload.ticket_key)) {',
      "  console.error('[gateway-publish] Refused: this pipeline_retry payload is not valid, so nothing was published:');",
      "  console.error('  - ticket_key must be a non-empty string for a \"pipeline_retry\" message');",
      '  process.exit(1);',
      '}',
      "fs.appendFileSync(process.env.PUBLISH_LOG, payload.ticket_key + '\\n');",
      "console.log('Accepted: ' + payload.ticket_key);",
      '',
    ].join('\n'));
  }

  const bin = path.join(dir, 'bin');
  fs.mkdirSync(bin);
  fs.writeFileSync(path.join(bin, 'curl'), '#!/bin/sh\nexit 0\n');
  fs.chmodSync(path.join(bin, 'curl'), 0o755);

  const script = path.join(dir, 'post-failure.sh');
  fs.writeFileSync(script, postFailureShellScript().split('/agent-docs').join(agentDocs));

  return { dir, script, bin, publishLog: path.join(dir, 'published.txt'), marker: path.join(dir, 'marker') };
}

function runLoop(run, { tickets, buildUrl = 'https://ci.test/job/dev/42/' }) {
  fs.writeFileSync(run.publishLog, '');
  const result = spawnSync('sh', ['-xe', run.script], {
    cwd: run.dir,
    encoding: 'utf8',
    env: {
      PATH: `${run.bin}:${process.env.PATH}`,
      NODE_PATH: redisModulePath(),
      FAILED_TICKETS: tickets.join(' '),
      FAILURE_TEXT: 'Pipeline failed on the post-merge dev build.',
      RETRY_FAILURE_MARKER: run.marker,
      PUBLISH_LOG: run.publishLog,
      PROJECT_NAME: 'hello-world',
      BUILD_URL: buildUrl,
      BUILD_NUMBER: '42',
      JIRA_URL: 'https://jira.test',
      JIRA_EMAIL: 'ci@test',
      JIRA_TOKEN: 'token',
    },
  });
  return {
    status: result.status,
    output: `${result.stdout}\n${result.stderr}`,
    marker: fs.existsSync(run.marker) ? fs.readFileSync(run.marker, 'utf8').split('\n').filter(Boolean) : null,
    published: fs.readFileSync(run.publishLog, 'utf8').split('\n').filter(Boolean),
  };
}

// ---------------------------------------------------------------------------
// The script block's half — asserted against the template's own text
// ---------------------------------------------------------------------------

test('REQ-03: the failure handler marks the build UNSTABLE when, and only when, the marker exists', () => {
  assert.match(POST_BLOCK, /"RETRY_FAILURE_MARKER=\.pipeline-retry-failures"/,
    'the marker path is passed into the shell through withEnv');
  assert.match(POST_BLOCK, /if \(fileExists\(env\.RETRY_FAILURE_MARKER\)\) \{/,
    'the UNSTABLE result is conditional on the marker existing');
  assert.match(POST_BLOCK, /readFile\(env\.RETRY_FAILURE_MARKER\)/,
    'the tickets that failed are read back out of the marker');
  assert.match(POST_BLOCK, /currentBuild\.result = 'UNSTABLE'/,
    'the build result is set to UNSTABLE, not left green');
  assert.ok(
    POST_BLOCK.indexOf("currentBuild.result = 'UNSTABLE'") > POST_BLOCK.indexOf('if (fileExists(env.RETRY_FAILURE_MARKER))'),
    'UNSTABLE is inside the marker-exists branch'
  );
});

test('REQ-03: the failure handler never aborts — no error() and no exit in the per-ticket loop', () => {
  assert.doesNotMatch(POST_BLOCK_CODE, /\berror\(/,
    "error() would swallow every later ticket's retry signal (Jenkinsfile.template's own best-effort guarantee)");
  const script = postFailureShellScript();
  assert.doesNotMatch(script, /\bexit\b/, 'the loop must not exit early on one ticket');
  assert.match(script, /\|\| \{ echo "WARN: could not publish pipeline_retry for \$ticket" >&2; echo "\$ticket" >> "\$RETRY_FAILURE_MARKER"; \}/,
    'a failed construction warns and records the ticket, then the loop continues');
  assert.match(script, /^\s*rm -f "\$RETRY_FAILURE_MARKER"/m,
    'the marker is cleared before the loop, so it only ever describes this run');
});

test('REQ-06: the loop still publishes through the commons raw entry point', () => {
  assert.match(postFailureShellScript(), /node \/agent-docs\/commons\/tools\/gateway-publish\.js "\$PROJECT_NAME" -/,
    'the payload is piped to the raw file/stdin entry point at its commons path');
});

// ---------------------------------------------------------------------------
// The loop's half — executed
// ---------------------------------------------------------------------------

test('REQ-03: the real publish tool refuses every malformed payload, the loop visits every ticket, and the marker names them all', () => {
  const run = prepareRun({ realTool: true });
  // An empty BUILD_URL is what a broken interpolation produces; the real tool
  // refuses the resulting payload before it opens any connection.
  const result = runLoop(run, { tickets: ['HW-1', 'HW-2', 'HW-3'], buildUrl: '' });

  assert.equal(result.status, 0, `the loop must finish: ${result.output}`);
  assert.match(result.output, /build_url must be a non-empty string when present on a "pipeline_retry" message/,
    "the tool's own field-naming message is in the build output");
  assert.match(result.output, /WARN: could not publish pipeline_retry for HW-1/);
  assert.deepEqual(result.marker, ['HW-1', 'HW-2', 'HW-3'],
    'every ticket was attempted and every failure recorded — the loop did not stop at the first');
});

test('REQ-03: one ticket\'s malformed payload does not cost the others their retry signal', () => {
  const run = prepareRun({ realTool: false, stubExitsFor: ['HW-2'] });
  const result = runLoop(run, { tickets: ['HW-1', 'HW-2', 'HW-3'] });

  assert.equal(result.status, 0, `the loop must finish: ${result.output}`);
  assert.deepEqual(result.marker, ['HW-2'], 'only the failing ticket is recorded');
  assert.deepEqual(result.published, ['HW-1', 'HW-3'],
    'the ticket after the failing one still had its pipeline_retry published');
  assert.match(result.output, /ticket_key must be a non-empty string/);
});

test('REQ-03: a clean run leaves no marker, so the build is not marked UNSTABLE', () => {
  const run = prepareRun({ realTool: false });
  const result = runLoop(run, { tickets: ['HW-1', 'HW-2'] });

  assert.equal(result.status, 0, result.output);
  assert.equal(result.marker, null, 'no marker means the script block leaves the build result alone');
  assert.deepEqual(result.published, ['HW-1', 'HW-2']);
});

test('REQ-03: a marker left behind by an earlier build does not make this one UNSTABLE', () => {
  const run = prepareRun({ realTool: false });
  fs.writeFileSync(run.marker, 'HW-99\n');
  const result = runLoop(run, { tickets: ['HW-1'] });

  assert.equal(result.status, 0, result.output);
  assert.equal(result.marker, null, "the loop's `rm -f` cleared the stale marker");
  assert.deepEqual(result.published, ['HW-1']);
});
