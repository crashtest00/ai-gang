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
 *  - the shell's half is *executed*. The `sh` script is extracted from
 *    setup/Jenkinsfile.template verbatim and run under `sh -xe`, the way
 *    Jenkins runs it, with one substitution: the `/agent-docs` bind mount
 *    becomes a temporary directory.
 *
 * Run A uses the real publish tool, whose refusal path never opens a Redis
 * connection, so the real field-naming message and the real non-zero exit are
 * what the shell sees. Runs B and C stub the tool, because a *successful*
 * publish needs a Redis this suite has none of; the tool's own accept/refuse
 * behaviour against real Redis is covered by
 * setup/commons/tools/a2a-validate.test.js.
 *
 * **From v5.2 the handler publishes one message, not one per ticket**
 * (Canonical Delivery State REQ-01). The message carries the promoted pull
 * requests in place of the tracker key the deleted tracker-key regex used to
 * find; `core` resolves each reference to a work item, appends the failure
 * comment there, and publishes a retry per work item with the canonical id.
 * So there is no per-ticket loop left to prove visits every ticket, and no
 * tracker `curl` left to stub — what remains is the construction, the marker,
 * and the UNSTABLE result.
 *
 * The `timeout` step that bounds the whole handler (V5.0 audit row 9) is
 * asserted the same way the `script` block's half is — against the template's
 * own text, plus a brace match proving the `sh` block and the marker check are
 * both inside it. Only a Jenkins instance can prove the step actually fires.
 *
 * What remains unproved without a Jenkins instance: that Jenkins' own
 * `fileExists`/`readFile`/`currentBuild.result`/`timeout` steps behave as
 * written, and that a build ends UNSTABLE rather than green or red.
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

// The body of the `{ ... }` block opened at or after `from`, by brace match.
// Groovy has to balance its braces to parse at all, and the brace-bearing
// string literals inside this handler (`jq` filters, the shell's `|| { ...; }`)
// each balance too, so a plain count is enough to say what a step encloses.
function blockBodyAt(text, from) {
  const open = text.indexOf('{', from);
  assert.notEqual(open, -1, 'the step must open a block');
  let depth = 0;
  for (let i = open; i < text.length; i++) {
    if (text[i] === '{') depth++;
    else if (text[i] === '}' && --depth === 0) return text.slice(open + 1, i);
  }
  throw new Error('unbalanced braces in the post block');
}

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
    // A stand-in for the durable write only: it refuses a payload naming any
    // of the pull requests in `stubExitsFor` the way the real tool refuses a
    // malformed payload — a field-naming message on stderr and a non-zero
    // exit — and records the rest as published.
    fs.writeFileSync(path.join(toolsDir, 'gateway-publish.js'), [
      '#!/usr/bin/env node',
      "const fs = require('fs');",
      "const payload = JSON.parse(fs.readFileSync(0, 'utf8'));",
      `const refuse = ${JSON.stringify(stubExitsFor)};`,
      'if ((payload.pull_requests || []).some(r => refuse.includes(r))) {',
      "  console.error('[gateway-publish] Refused: this pipeline_retry payload is not valid, so nothing was published:');",
      "  console.error('  - pull_requests must be an array of pull-request URLs for a \"pipeline_retry\" message');",
      '  process.exit(1);',
      '}',
      "fs.appendFileSync(process.env.PUBLISH_LOG, JSON.stringify(payload.pull_requests) + '\\n');",
      "console.log('Accepted: ' + (payload.pull_requests || []).join(' '));",
      '',
    ].join('\n'));
  }

  const bin = path.join(dir, 'bin');
  fs.mkdirSync(bin);

  const script = path.join(dir, 'post-failure.sh');
  fs.writeFileSync(script, postFailureShellScript().split('/agent-docs').join(agentDocs));

  return { dir, script, bin, publishLog: path.join(dir, 'published.txt'), marker: path.join(dir, 'marker') };
}

function runLoop(run, { pullRequests, buildUrl = 'https://ci.test/job/dev/42/' }) {
  fs.writeFileSync(run.publishLog, '');
  const result = spawnSync('sh', ['-xe', run.script], {
    cwd: run.dir,
    encoding: 'utf8',
    env: {
      PATH: `${run.bin}:${process.env.PATH}`,
      NODE_PATH: redisModulePath(),
      FAILED_PRS: pullRequests.join(' '),
      FAILURE_TEXT: 'Pipeline failed on the post-merge dev build.',
      RETRY_FAILURE_MARKER: run.marker,
      PUBLISH_LOG: run.publishLog,
      PROJECT_NAME: 'hello-world',
      BUILD_URL: buildUrl,
      BUILD_NUMBER: '42',
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

test('row 9: the failure handler runs under a timeout step, so an unreachable Redis cannot hang the build for ever', () => {
  // Redis unreachable used to mean gateway-publish.js retried for ever inside
  // this `sh`, with no bound anywhere: not on the step, not in `options`, and
  // `disableConcurrentBuilds()` then queues every later dev build behind it.
  assert.doesNotMatch(TEMPLATE.slice(TEMPLATE.indexOf('options {'), TEMPLATE.indexOf('environment {')), /timeout\(/,
    'the pipeline options set no global timeout, so the handler needs its own');

  const at = POST_BLOCK_CODE.search(/timeout\(time: \d+, unit: '(SECONDS|MINUTES|HOURS)'\)/);
  assert.notEqual(at, -1, "the failure handler must be wrapped in a Jenkins `timeout` step");

  const bounded = blockBodyAt(POST_BLOCK_CODE, at);
  assert.match(bounded, /withEnv\(\[/, 'the timeout encloses the withEnv block, not just part of it');
  assert.ok(bounded.includes("sh '''"), 'the per-ticket shell loop runs inside the timeout');
  assert.ok(bounded.includes('fileExists(env.RETRY_FAILURE_MARKER)'),
    'the marker check runs inside the timeout too');
  assert.ok(bounded.includes("currentBuild.result = 'UNSTABLE'"),
    'so does the UNSTABLE result it sets');

  // Minutes, not hours: the point is that a queued dev build waits minutes at
  // worst. The publish tool itself gives up in seconds (its own
  // reconnectStrategy), so this only ever catches something else hanging.
  const [, value, unit] = POST_BLOCK_CODE.match(/timeout\(time: (\d+), unit: '(SECONDS|MINUTES|HOURS)'\)/);
  assert.equal(unit, 'MINUTES');
  assert.ok(Number(value) <= 15, `the handler's bound must stay small, got ${value} ${unit}`);
});

test('row 9: both commons publish entry points bound their own Redis connection', () => {
  for (const tool of ['gateway-publish.js', 'a2a-submit.js']) {
    const source = fs.readFileSync(path.join(TOOLS_DIR, tool), 'utf8');
    assert.match(source, /connectTimeout/, `${tool} must bound the TCP connect`);
    assert.match(source, /reconnectStrategy/, `${tool} must bound the retry loop`);
    assert.match(source, /socket: boundedSocket\(redisUrl\)/,
      `${tool} must pass those options to createClient, not merely define them`);
  }
});

test('REQ-03: the failure handler never aborts — no error() and no exit in the shell', () => {
  assert.doesNotMatch(POST_BLOCK_CODE, /\berror\(/,
    "error() would swallow the retry signal (Jenkinsfile.template's own best-effort guarantee)");
  const script = postFailureShellScript();
  assert.doesNotMatch(script, /\bexit\b/, 'the shell must not exit early');
  assert.match(script, /\|\| \{ echo "WARN: could not publish pipeline_retry for \$FAILED_PRS" >&2; echo "\$FAILED_PRS" >> "\$RETRY_FAILURE_MARKER"; \}/,
    'a failed construction warns and records the references, then the handler finishes');
  assert.match(script, /^\s*rm -f "\$RETRY_FAILURE_MARKER"/m,
    'the marker is cleared first, so it only ever describes this run');
});

test('V5.2 REQ-01: the failure handler names pull requests and no tracker key', () => {
  const script = postFailureShellScript();
  assert.match(script, /pull_requests:\$prs/, 'the message carries the promoted pull requests');
  // Positive shape, not an absence check: the payload `jq` builds is exactly
  // these five fields (type, pull_requests, failure_text, build_url,
  // build_number) and nothing else — proving the PR-based shape without
  // naming any retired tracker literal, which a grep for one would otherwise
  // find inside this very assertion.
  assert.match(script,
    /'\{type:"pipeline_retry",pull_requests:\$prs,failure_text:\$t,build_url:\$u,build_number:\$n\}'/,
    'the payload is built from exactly these five fields, with no further field appended');
  assert.doesNotMatch(script, /\bcurl\b/,
    'Jenkins writes to no tracker here — core appends the failure comment through its own comment path');
  assert.match(TEMPLATE, /FAILED_PRS=\$\{promotedPrs\.join\(' '\)\}/,
    "the handler's one input is the promoted pull-request list built above, not a second, tracker-shaped variable");
  assert.doesNotMatch(TEMPLATE, /\[A-Z\]\[A-Z0-9\]\+-\[0-9\]\+/,
    'both tracker-key regex sites are removed, not relocated');
});

test('V5.2 REQ-01: the dev build publishes one beta_deployed event, and a failed publish marks it UNSTABLE', () => {
  const stage = TEMPLATE.slice(TEMPLATE.indexOf("stage('Publish the beta deployment')"), TEMPLATE.indexOf('post {'));
  assert.ok(stage.length > 0, 'the dev build must have a beta-deployment publish stage');

  // REQ-01's payload: the promoted pull requests, the deployed SHA, the
  // build identifier, the build url, and the beta url Jenkins builds from
  // the project name and core cannot derive.
  assert.match(stage, /type:"beta_deployed"/);
  for (const field of ['pull_requests:\\$prs', 'deployed_sha:\\$sha', 'build_identifier:\\$build',
                        'build_url:\\$url', 'beta_url:\\$beta']) {
    assert.match(stage, new RegExp(field), `the event must carry ${field}`);
  }
  assert.match(stage, /node \/agent-docs\/commons\/tools\/gateway-publish\.js "\$PROJECT_NAME" -/,
    'published through the commons raw entry point, which validates it before the write');

  // The failed publish is caught in the shell and the stage then sets
  // UNSTABLE, so it never triggers the failure handler's pipeline_retry.
  assert.match(stage, /\|\| \{ echo "WARN: could not publish beta_deployed/);
  assert.match(stage, /if \(fileExists\(env\.PUBLISH_FAILURE_MARKER\)\) \{/);
  assert.match(stage, /currentBuild\.result = 'UNSTABLE'/);
  assert.doesNotMatch(stage, /\berror\(/, 'a failed publish must not fail the build');
  assert.doesNotMatch(stage, /\bcurl\b/, 'no tracker write survives in the dev build');
});

test('REQ-06: the loop still publishes through the commons raw entry point', () => {
  assert.match(postFailureShellScript(), /node \/agent-docs\/commons\/tools\/gateway-publish\.js "\$PROJECT_NAME" -/,
    'the payload is piped to the raw file/stdin entry point at its commons path');
});

// ---------------------------------------------------------------------------
// The loop's half — executed
// ---------------------------------------------------------------------------

const PR_1 = 'https://github.com/org/repo/pull/1';
const PR_2 = 'https://github.com/org/repo/pull/2';

test('REQ-03: the real publish tool refuses a malformed payload, the handler finishes, and the marker records it', () => {
  const run = prepareRun({ realTool: true });
  // An empty BUILD_URL is what a broken interpolation produces; the real tool
  // refuses the resulting payload before it opens any connection.
  const result = runLoop(run, { pullRequests: [PR_1, PR_2], buildUrl: '' });

  assert.equal(result.status, 0, `the handler must finish: ${result.output}`);
  assert.match(result.output, /build_url must be a non-empty string when present on a "pipeline_retry" message/,
    "the tool's own field-naming message is in the build output");
  assert.match(result.output, /WARN: could not publish pipeline_retry/);
  assert.deepEqual(result.marker, [`${PR_1} ${PR_2}`],
    'the references that went unpublished are recorded, so the script block can name them');
});

test('V5.2 REQ-01: the published message carries every promoted pull request in one payload', () => {
  const run = prepareRun({ realTool: false });
  const result = runLoop(run, { pullRequests: [PR_1, PR_2] });

  assert.equal(result.status, 0, result.output);
  assert.equal(result.marker, null, 'no marker means the script block leaves the build result alone');
  assert.deepEqual(result.published, [JSON.stringify([PR_1, PR_2])],
    'one message, naming both — core resolves each reference and publishes a retry per work item');
});

test("V5.2 REQ-01: a reference the tool refuses costs the whole message, and the marker says so", () => {
  const run = prepareRun({ realTool: false, stubExitsFor: [PR_2] });
  const result = runLoop(run, { pullRequests: [PR_1, PR_2] });

  assert.equal(result.status, 0, `the handler must finish: ${result.output}`);
  assert.deepEqual(result.marker, [`${PR_1} ${PR_2}`]);
  assert.deepEqual(result.published, [], 'nothing was published');
  assert.match(result.output, /pull_requests must be an array of pull-request URLs/);
});

test('REQ-03: a marker left behind by an earlier build does not make this one UNSTABLE', () => {
  const run = prepareRun({ realTool: false });
  fs.writeFileSync(run.marker, 'https://github.com/org/repo/pull/99\n');
  const result = runLoop(run, { pullRequests: [PR_1] });

  assert.equal(result.status, 0, result.output);
  assert.equal(result.marker, null, "the handler's `rm -f` cleared the stale marker");
  assert.deepEqual(result.published, [JSON.stringify([PR_1])]);
});
