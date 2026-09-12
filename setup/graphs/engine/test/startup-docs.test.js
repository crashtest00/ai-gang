'use strict';

// The operator documentation against the code it describes.
//
// docs/ClaudeInstructions.md's platform-startup section enumerates the
// initialization steps, and docs/UserGuide.md tells an operator which
// file to fill in and which address to open. Every one of those is free
// text, and every one of them has a definition elsewhere in the
// repository. This checks the docs against those definitions, not against
// each other.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const REPO_ROOT = path.join(__dirname, '..', '..', '..', '..');
const STARTUP_DIR = path.join(REPO_ROOT, 'scripts', 'startup');
const CLAUDE_DOC = path.join(REPO_ROOT, 'docs', 'ClaudeInstructions.md');
const USER_GUIDE = path.join(REPO_ROOT, 'docs', 'UserGuide.md');

function read(file) {
  return fs.readFileSync(file, 'utf8');
}

function run(script, ...args) {
  const result = spawnSync('bash', [path.join(STARTUP_DIR, script), ...args], {
    encoding: 'utf8',
    timeout: 15000,
  });
  assert.equal(result.status, 0, result.stderr);
  return result.stdout;
}

function stepRows() {
  return run('steps.sh', 'list').trim().split('\n').map((line) => {
    const [number, id, script, description] = line.split('|');
    return { number, id, script, description };
  });
}

// ---- the ordered steps ----

test('the platform-startup section lists every step, by script and description', () => {
  const doc = read(CLAUDE_DOC);
  for (const step of stepRows()) {
    assert.ok(doc.includes(`\`${step.script}\``), `${step.script} is not named in ClaudeInstructions.md`);
    assert.ok(
      doc.includes(step.description),
      `the description of step ${step.number} (${step.id}) is not in ClaudeInstructions.md`
    );
  }
});

test('the section lists no step that does not exist', () => {
  const doc = read(CLAUDE_DOC);
  const known = new Set(stepRows().map((s) => s.script));
  // Every scripts/startup/<name>.sh the doc mentions is either one of the
  // ordered steps or one of the entrypoint's own pre-flight scripts.
  const preflight = new Set([
    'scripts/startup/entrypoint.sh',
    'scripts/startup/validate-config.sh',
    'scripts/startup/validate-env.sh',
    'scripts/startup/config-identity.sh',
    'scripts/startup/derive-env.sh',
    'scripts/startup/steps.sh',
    'scripts/startup/status.sh',
  ]);
  const mentioned = doc.match(/scripts\/startup\/[a-z-]+\.sh/g) || [];
  for (const name of new Set(mentioned)) {
    assert.ok(known.has(name) || preflight.has(name), `${name} is documented but is not a startup script`);
    assert.ok(fs.existsSync(path.join(REPO_ROOT, name)), `${name} is documented but does not exist`);
  }
});

test('every pre-flight script the section names exists and is executable', () => {
  for (const name of ['entrypoint.sh', 'validate-config.sh', 'validate-env.sh', 'config-identity.sh', 'derive-env.sh']) {
    const full = path.join(STARTUP_DIR, name);
    assert.ok(fs.existsSync(full), `${name} is missing`);
    assert.ok(fs.statSync(full).mode & 0o111, `${name} is not executable`);
  }
});

// ---- the records and the address ----

test('both documents give the Django admin address the status record actually writes', () => {
  const adminUrl = run('status.sh', 'admin-url').trim();
  assert.ok(read(CLAUDE_DOC).includes(adminUrl), `ClaudeInstructions.md does not name ${adminUrl}`);
  assert.ok(read(USER_GUIDE).includes(adminUrl), `UserGuide.md does not name ${adminUrl}`);
});

test('the admin address matches where the service is actually published and mounted', () => {
  const adminUrl = run('status.sh', 'admin-url').trim();
  const compose = read(path.join(REPO_ROOT, 'services', 'work-item-service', 'docker-compose.yml'));
  const urls = read(path.join(REPO_ROOT, 'services', 'work-item-service', 'workitemservice', 'urls.py'));
  assert.match(compose, /"127\.0\.0\.1:9100:9100"/);
  assert.match(urls, /path\('django-admin\/', admin\.site\.urls\)/);
  assert.equal(adminUrl, 'http://127.0.0.1:9100/django-admin/');
});

test('the record filenames the documents name are the ones the scripts write', () => {
  const doc = read(CLAUDE_DOC);
  for (const name of ['.ai-gang/status.json', '.ai-gang/config-identity.json', '.ai-gang/startup.log']) {
    assert.ok(doc.includes(name), `${name} is not documented`);
  }
  assert.match(read(path.join(STARTUP_DIR, 'status.sh')), /STATUS_FILE="\$AIGANG_STATE_DIR\/status\.json"/);
  assert.match(read(path.join(STARTUP_DIR, 'lib.sh')), /AIGANG_IDENTITY_FILE="\$AIGANG_STATE_DIR\/config-identity\.json"/);
  assert.match(read(path.join(STARTUP_DIR, 'lib.sh')), /AIGANG_LOG_FILE="\$AIGANG_STATE_DIR\/startup\.log"/);
});

// ---- the configuration and environment files an operator fills in ----

test('the UserGuide names the two files an operator copies, and both are shipped', () => {
  const guide = read(USER_GUIDE);
  assert.ok(guide.includes('ai-gang.config.template.json'));
  assert.ok(guide.includes('.env.template'));
  assert.ok(fs.existsSync(path.join(REPO_ROOT, 'ai-gang.config.template.json')));
  assert.ok(fs.existsSync(path.join(REPO_ROOT, '.env.template')));
});

test("the UserGuide's example configuration is one the validator accepts", () => {
  const guide = read(USER_GUIDE);
  const block = guide.match(/```json\n([\s\S]*?)```/);
  assert.ok(block, 'the UserGuide should show an example configuration');
  const { validatePlatformConfigText } = require('../lib/config/validate');
  const result = validatePlatformConfigText(block[1]);
  assert.equal(result.valid, true, `the documented example is not valid: ${result.errors.join(' | ')}`);
});

test('the services the documents say startup brings up are the ones it checks', () => {
  // confirm-health.sh is the definition of "AI Gang is up".
  const health = read(path.join(STARTUP_DIR, 'confirm-health.sh'));
  for (const service of ['redis', 'work-item-service', 'scrummaster']) {
    assert.ok(health.includes(`check ${service} `), `confirm-health.sh does not check ${service}`);
  }
  assert.match(health, /check "\$PROJECT_NAME" project_ok/);

  // Markdown wraps, so the guide is compared with its line breaks
  // flattened rather than as written.
  const guide = read(USER_GUIDE).replace(/\s+/g, ' ');
  assert.ok(guide.includes('work-item service'));
  assert.ok(guide.includes('ScrumMaster'));
  assert.ok(guide.includes('Redis'));
  // Jenkins is not part of this flow and must not be claimed as part of it.
  assert.match(guide, /\*\*Jenkins\*\*[^.]*is a later addition/);
});

test('the phases the section claims to cover, and those it excludes, both exist in the document', () => {
  const doc = read(CLAUDE_DOC);
  for (const heading of ['### 2.1 Redis', '### 2.2 ScrumMaster', '### 3.0 Create Project Repo',
    '### 3.1 Project Initialisation', '### 3.2 Container Setup', '### 3.5 Start the Redis Subscriber',
    '### 2.0 Cloudflare Tunnel', '### 2.3 Jenkins', '### 3.6 Jenkins Pipeline', '### 3.7 Release Promotion']) {
    assert.ok(doc.includes(heading), `the section refers to a phase with no heading: ${heading}`);
  }
});

test("the Django-admin end-to-end leg is its own section, not an edit to Phase 4's Jira one", () => {
  const doc = read(CLAUDE_DOC);
  assert.ok(doc.includes('## Phase 4: End-to-End Test'), 'Phase 4 must still be there, unchanged in kind');
  assert.ok(doc.includes('## End-to-End Test: Platform Startup'));
  const phase4 = doc.slice(doc.indexOf('## Phase 4: End-to-End Test'), doc.indexOf('## End-to-End Test: Platform Startup'));
  assert.match(phase4, /In Jira, create a story/, "Phase 4's Jira scenario must be left as it was");
});

test('the end-to-end leg uses the work-item type and status the service actually enforces', () => {
  const doc = read(CLAUDE_DOC);
  const leg = doc.slice(doc.indexOf('## End-to-End Test: Platform Startup'));
  // dispatchConsumer.js dispatches on status 'ready' with an assignee.
  const dispatch = read(path.join(REPO_ROOT, 'services', 'scrummaster', 'src', 'dispatchConsumer.js'));
  assert.match(dispatch, /full\.status !== 'ready'/);
  assert.match(dispatch, /if \(!full\.assignee_agent_id\) return;/);
  assert.ok(leg.includes('`ready`'));
  assert.ok(leg.includes('`refinement-agent`'));
  assert.ok(leg.includes('`story`'));
  // store.py requires story detail before a story may leave 'proposed'.
  const store = read(path.join(REPO_ROOT, 'services', 'work-item-service', 'workitems', 'store.py'));
  assert.match(store, /if item\.type == 'story' and validity\['baseline'\] != 'proposed'/);
  assert.match(leg.replace(/\s+/g, ' '), /story fields .{0,40}(before|and moving)/i,
    'the leg should say the story fields have to be saved before the status moves');

  // The admin's story-detail inline is hidden on the add form, which is
  // why the sequence takes three saves rather than one.
  const admin = read(path.join(REPO_ROOT, 'services', 'work-item-service', 'workitems', 'admin.py'));
  assert.match(admin, /def get_inlines\(self, request, obj\)/);
  assert.match(admin, /WorkItemStoryDetailInline/);
  assert.match(leg.replace(/\s+/g, ' '), /three saves/);

  // external_key routes dispatch through Jira, which this flow does not
  // set up, so the leg has to say to leave it empty.
  const dispatchSource = read(path.join(REPO_ROOT, 'services', 'scrummaster', 'src', 'dispatchConsumer.js'));
  assert.match(dispatchSource, /if \(full\.external_key\) \{\n\s+return jira\.getIssue/);
  assert.match(leg.replace(/\s+/g, ' '), /External key.{0,40}leave it empty/i);
});

// ---- the work-item service's own environment example ----

test('every variable the .env.example declares is one settings.py reads', () => {
  const example = read(path.join(REPO_ROOT, 'services', 'work-item-service', '.env.example'));
  const settings = read(path.join(REPO_ROOT, 'services', 'work-item-service', 'workitemservice', 'settings.py'));
  const declared = [...example.matchAll(/^#?\s*([A-Z][A-Z0-9_]*)=/gm)].map((m) => m[1]);
  assert.ok(declared.length > 0);
  for (const name of new Set(declared)) {
    assert.ok(
      settings.includes(`'${name}'`),
      `${name} is in services/work-item-service/.env.example but settings.py never reads it`
    );
  }
});

test('the variables derive-env.sh actually writes are all declared in the .env.example', () => {
  const example = read(path.join(REPO_ROOT, 'services', 'work-item-service', '.env.example'));
  const derive = read(path.join(STARTUP_DIR, 'derive-env.sh'));
  const written = [...derive.matchAll(/echo "([A-Z][A-Z0-9_]*)=/g)].map((m) => m[1]);
  assert.ok(written.length > 0);
  for (const name of new Set(written)) {
    assert.match(
      example,
      new RegExp(`^#?\\s*${name}=`, 'm'),
      `derive-env.sh writes ${name} but the .env.example does not declare it`
    );
  }
});
