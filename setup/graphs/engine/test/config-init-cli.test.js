'use strict';

// Drives the real scripts/init-project.sh --config entrypoint end to end
// (rather than only unit-testing the validation function it calls
// internally — see config-validate.test.js for that faster, more
// exhaustive coverage) so that invalid input is proven to produce no
// project registration and no provisioning side effect through the actual
// dispatch path, not just through the function that decides validity.
//
// Isolation: every run gets its own temp "projects" directory and its own
// temp services/scrummaster/config/projects.json fixture via the
// AIGANG_PROJECTS_DIR / AIGANG_PROJECTS_CONFIG overrides added to
// scripts/init-project.sh for exactly this purpose, plus a HQ_ENV pointed
// at a nonexistent path so the real ~/ai-gang/.env (if any exists on this
// host) is never sourced. No network call is made: GITHUB_URL is always
// left blank, and --connect-jira is never passed, so no Jira/GitHub
// service is ever contacted.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync, spawn } = require('node:child_process');
const { validateConfigFile } = require('../lib/config/validate');

const REPO_ROOT = path.join(__dirname, '..', '..', '..', '..');
const SCRIPT_PATH = path.join(REPO_ROOT, 'scripts', 'init-project.sh');
const EXAMPLE_CONFIG_PATH = path.join(REPO_ROOT, 'scripts', 'init-project.example.json');

const VALID_CONFIG = {
  schemaVersion: 1,
  project: { name: 'acceptance-project', type: 'web', stack: 'node-express' },
};

function makeTempDir(prefix) {
  return fs.mkdtempSync(path.join(os.tmpdir(), prefix));
}

function makeIsolatedEnv(extra = {}) {
  const root = makeTempDir('aigang-config-init-');
  const projectsDir = path.join(root, 'projects');
  fs.mkdirSync(projectsDir, { recursive: true });
  const projectsConfigPath = path.join(root, 'projects.json');
  fs.writeFileSync(projectsConfigPath, JSON.stringify({ projects: [] }, null, 2));
  const env = {
    ...process.env,
    AIGANG_PROJECTS_DIR: projectsDir,
    AIGANG_PROJECTS_CONFIG: projectsConfigPath,
    HQ_ENV: path.join(root, 'nonexistent.env'),
    ...extra,
  };
  return { root, projectsDir, projectsConfigPath, env };
}

function writeConfigFile(root, name, content) {
  const filePath = path.join(root, name);
  if (typeof content === 'string') {
    fs.writeFileSync(filePath, content);
  } else if (Buffer.isBuffer(content)) {
    fs.writeFileSync(filePath, content);
  } else {
    fs.writeFileSync(filePath, JSON.stringify(content));
  }
  return filePath;
}

// stdin for a successful run: blank line answers the GitHub URL prompt,
// "y" answers the final "Continue? [y/N]" confirmation. Neither is one of
// the three config-driven decisions (name/type/stack), so both still
// prompt regardless of --config — only the prompts for those three
// decisions themselves are required to disappear.
const CONFIRM_STDIN = '\ny\n';

function runScript(args, { env, input = CONFIRM_STDIN } = {}) {
  const result = spawnSync('bash', [SCRIPT_PATH, ...args], {
    cwd: REPO_ROOT,
    env,
    input,
    encoding: 'utf8',
    timeout: 15000,
  });
  return result;
}

function readProjectsConfig(projectsConfigPath) {
  return JSON.parse(fs.readFileSync(projectsConfigPath, 'utf8'));
}

// ---- explicit, versioned config input ----

test('a valid config initializes the project through the real entrypoint', () => {
  const { root, projectsDir, projectsConfigPath, env } = makeIsolatedEnv();
  const configPath = writeConfigFile(root, 'config.json', VALID_CONFIG);

  const result = runScript(['--config', configPath], { env });

  assert.equal(result.status, 0, result.stderr + result.stdout);
  const projectDir = path.join(projectsDir, 'acceptance-project');
  assert.ok(fs.existsSync(projectDir), 'project directory should be created');
  assert.ok(fs.existsSync(path.join(projectDir, 'src', 'package.json')), 'web boilerplate should be scaffolded');

  const registry = readProjectsConfig(projectsConfigPath);
  assert.ok(registry.projects.some((p) => p.name === 'acceptance-project'));
});

test('the shipped example config passes validation and drives the real entrypoint to success', () => {
  const validation = validateConfigFile(EXAMPLE_CONFIG_PATH);
  assert.equal(validation.valid, true, (validation.errors || []).join('; '));

  const { projectsDir, projectsConfigPath, env } = makeIsolatedEnv();

  const result = runScript(['--config', EXAMPLE_CONFIG_PATH], { env });

  assert.equal(result.status, 0, result.stderr + result.stdout);
  const projectName = validation.decisions.name;
  assert.ok(fs.existsSync(path.join(projectsDir, projectName)), 'project directory should be created');
  const registry = readProjectsConfig(projectsConfigPath);
  assert.ok(registry.projects.some((p) => p.name === projectName));
});

test('a missing config file is rejected before any project creation', () => {
  const { root, projectsDir, env } = makeIsolatedEnv();
  const configPath = path.join(root, 'does-not-exist.json');

  const result = runScript(['--config', configPath], { env });

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /not found/);
  assert.deepEqual(fs.readdirSync(projectsDir), []);
});

test('malformed JSON is rejected before any project creation', () => {
  const { root, projectsDir, env } = makeIsolatedEnv();
  const configPath = writeConfigFile(root, 'config.json', '{ this is not json');

  const result = runScript(['--config', configPath], { env });

  assert.notEqual(result.status, 0);
  assert.deepEqual(fs.readdirSync(projectsDir), []);
});

test('a duplicate key is rejected before any project creation, even though JSON.parse alone would accept it', () => {
  const { root, projectsDir, env } = makeIsolatedEnv();
  const text = '{"schemaVersion":1,"project":{"name":"acceptance-project","type":"web","stack":"node-express","stack":"node-express"}}';
  const configPath = writeConfigFile(root, 'config.json', text);

  const result = runScript(['--config', configPath], { env });

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /duplicate key/);
  assert.deepEqual(fs.readdirSync(projectsDir), []);
});

test('an unknown top-level field is rejected before any project creation', () => {
  const { root, projectsDir, env } = makeIsolatedEnv();
  const configPath = writeConfigFile(root, 'config.json', { ...VALID_CONFIG, unexpected: true });

  const result = runScript(['--config', configPath], { env });

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /unknown field "unexpected"/);
  assert.deepEqual(fs.readdirSync(projectsDir), []);
});

test('an unsupported schema version is rejected before any project creation', () => {
  const { root, projectsDir, env } = makeIsolatedEnv();
  const configPath = writeConfigFile(root, 'config.json', { ...VALID_CONFIG, schemaVersion: 99 });

  const result = runScript(['--config', configPath], { env });

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /unsupported schemaVersion/);
  assert.deepEqual(fs.readdirSync(projectsDir), []);
});

test('a wrong-typed field is rejected before any project creation', () => {
  const { root, projectsDir, env } = makeIsolatedEnv();
  const configPath = writeConfigFile(root, 'config.json', {
    schemaVersion: 1,
    project: { name: 12345, type: 'web', stack: 'node-express' },
  });

  const result = runScript(['--config', configPath], { env });

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /"project.name" must be a nonempty string/);
  assert.deepEqual(fs.readdirSync(projectsDir), []);
});

test('a missing required field is rejected before any project creation', () => {
  const { root, projectsDir, env } = makeIsolatedEnv();
  const configPath = writeConfigFile(root, 'config.json', {
    schemaVersion: 1,
    project: { name: 'acceptance-project', type: 'web' },
  });

  const result = runScript(['--config', configPath], { env });

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /missing required field "project.stack"/);
  assert.deepEqual(fs.readdirSync(projectsDir), []);
});

test('non-UTF-8 config content is rejected before any project creation', () => {
  const { root, projectsDir, env } = makeIsolatedEnv();
  const invalidUtf8 = Buffer.concat([
    Buffer.from('{"schemaVersion":1,"project":{"name":"'),
    Buffer.from([0xff, 0xfe]),
    Buffer.from('","type":"web","stack":"node-express"}}'),
  ]);
  const configPath = writeConfigFile(root, 'config.json', invalidUtf8);

  const result = runScript(['--config', configPath], { env });

  assert.notEqual(result.status, 0);
  assert.deepEqual(fs.readdirSync(projectsDir), []);
});

test('shell syntax inside a config value is rejected as data, and never executes', () => {
  const { root, projectsDir, env } = makeIsolatedEnv();
  const sentinel = path.join(root, 'sentinel-should-not-exist');
  const configPath = writeConfigFile(root, 'config.json', {
    schemaVersion: 1,
    project: { name: 'acceptance-project', type: `web; touch ${sentinel}`, stack: 'node-express' },
  });

  const result = runScript(['--config', configPath], { env });

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /not a supported deployment target/);
  assert.equal(fs.existsSync(sentinel), false, 'shell syntax embedded in config data must never execute');
  assert.deepEqual(fs.readdirSync(projectsDir), []);
});

// ---- deterministic project decisions ----

test('an unknown type is rejected and the diagnostic lists supported choices', () => {
  const { root, env } = makeIsolatedEnv();
  const configPath = writeConfigFile(root, 'config.json', {
    schemaVersion: 1,
    project: { name: 'acceptance-project', type: 'spaceship', stack: 'node-express' },
  });

  const result = runScript(['--config', configPath], { env });

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /not a supported deployment target/);
  assert.match(result.stderr, /web/);
});

test('an unknown stack is rejected and the diagnostic lists supported choices', () => {
  const { root, env } = makeIsolatedEnv();
  const configPath = writeConfigFile(root, 'config.json', {
    schemaVersion: 1,
    project: { name: 'acceptance-project', type: 'web', stack: 'rust-actix' },
  });

  const result = runScript(['--config', configPath], { env });

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /not a supported stack profile/);
  assert.match(result.stderr, /node-express/);
});

test('an incompatible target/stack pair is rejected', () => {
  const { root, env } = makeIsolatedEnv();
  const configPath = writeConfigFile(root, 'config.json', {
    schemaVersion: 1,
    project: { name: 'acceptance-project', type: 'web', stack: 'python-flask' },
  });

  const result = runScript(['--config', configPath], { env });

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /not a supported stack profile for target "web"/);
});

test('an unsafe name is rejected before any project creation', () => {
  const { root, projectsDir, env } = makeIsolatedEnv();
  const configPath = writeConfigFile(root, 'config.json', {
    schemaVersion: 1,
    project: { name: 'Not_Safe', type: 'web', stack: 'node-express' },
  });

  const result = runScript(['--config', configPath], { env });

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /not a safe project name/);
  assert.deepEqual(fs.readdirSync(projectsDir), []);
});

test('no prompt is issued for name, type, or stack when a valid config is supplied', () => {
  const { root, env } = makeIsolatedEnv();
  const configPath = writeConfigFile(root, 'config.json', VALID_CONFIG);

  const result = runScript(['--config', configPath], { env });

  assert.equal(result.status, 0, result.stderr + result.stdout);
  const combined = result.stdout + result.stderr;
  assert.doesNotMatch(combined, /Deployment target \(web\/mobile/);
  assert.doesNotMatch(combined, /Project name \(lowercase/);
});

test('a valid config run twice in separate clean environments produces matching project metadata', () => {
  const configRoot = makeTempDir('aigang-config-init-shared-config-');
  const configPath = writeConfigFile(configRoot, 'config.json', VALID_CONFIG);

  const envA = makeIsolatedEnv();
  const envB = makeIsolatedEnv();

  const resultA = runScript(['--config', configPath], { env: envA.env });
  const resultB = runScript(['--config', configPath], { env: envB.env });

  assert.equal(resultA.status, 0, resultA.stderr);
  assert.equal(resultB.status, 0, resultB.stderr);

  const identityA = JSON.parse(
    fs.readFileSync(path.join(envA.projectsDir, 'acceptance-project', '.aigang-config-identity.json'), 'utf8')
  );
  const identityB = JSON.parse(
    fs.readFileSync(path.join(envB.projectsDir, 'acceptance-project', '.aigang-config-identity.json'), 'utf8')
  );
  const expectedIdentity = { schemaVersion: 1, ...VALID_CONFIG.project };
  assert.deepEqual(identityA, expectedIdentity);
  assert.deepEqual(identityB, expectedIdentity);
});

// ---- unambiguous precedence and preserved defaults ----

test('a matching --deployment value is accepted alongside --config', () => {
  const { root, env } = makeIsolatedEnv();
  const configPath = writeConfigFile(root, 'config.json', VALID_CONFIG);

  const result = runScript(['--config', configPath, '--deployment', 'web'], { env });

  assert.equal(result.status, 0, result.stderr + result.stdout);
});

test('a conflicting --deployment value is rejected before any mutation', () => {
  const { root, projectsDir, env } = makeIsolatedEnv();
  const configPath = writeConfigFile(root, 'config.json', VALID_CONFIG);

  const result = runScript(['--config', configPath, '--deployment', 'mobile'], { env });

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /conflicts with project\.type/);
  assert.deepEqual(fs.readdirSync(projectsDir), []);
});

test('a conflicting --desktop-framework value is rejected before any mutation', () => {
  const { root, projectsDir, env } = makeIsolatedEnv();
  const configPath = writeConfigFile(root, 'config.json', VALID_CONFIG);

  const result = runScript(['--config', configPath, '--desktop-framework', 'tauri'], { env });

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /conflicts with project\.type/);
  assert.deepEqual(fs.readdirSync(projectsDir), []);
});

test('an environment variable cannot override a configured decision', () => {
  const { root, projectsDir, env } = makeIsolatedEnv({ DEPLOYMENT: 'mobile' });
  const configPath = writeConfigFile(root, 'config.json', VALID_CONFIG);

  const result = runScript(['--config', configPath], { env });

  assert.equal(result.status, 0, result.stderr + result.stdout);
  const projectDir = path.join(projectsDir, 'acceptance-project');
  const identity = JSON.parse(fs.readFileSync(path.join(projectDir, '.aigang-config-identity.json'), 'utf8'));
  assert.equal(identity.type, 'web', 'the DEPLOYMENT environment variable must not override project.type');
});

test('without --config, the interactive flow still prompts and still works', () => {
  const { root, projectsDir, env } = makeIsolatedEnv();
  void root;

  const result = runScript(['--deployment', 'web'], {
    env,
    input: 'interactive-project\n\ny\n', // project name, blank GitHub URL, confirm
  });

  assert.equal(result.status, 0, result.stderr + result.stdout);
  assert.ok(fs.existsSync(path.join(projectsDir, 'interactive-project')));
});

test('config alone (no --connect-jira) creates no Jira project or connection', () => {
  const { root, projectsConfigPath, env } = makeIsolatedEnv();
  const configPath = writeConfigFile(root, 'config.json', VALID_CONFIG);

  const result = runScript(['--config', configPath], { env });

  assert.equal(result.status, 0, result.stderr + result.stdout);
  assert.doesNotMatch(result.stdout, /Checking Jira/);
  const registry = readProjectsConfig(projectsConfigPath);
  const entry = registry.projects.find((p) => p.name === 'acceptance-project');
  assert.equal(entry.jiraProjectKey, null);
});

// ---- stable input across retries ----

test('retrying with the same config is idempotent — no duplicate registration, no repeated prompts', () => {
  const { root, projectsDir, projectsConfigPath, env } = makeIsolatedEnv();
  const configPath = writeConfigFile(root, 'config.json', VALID_CONFIG);

  const first = runScript(['--config', configPath], { env });
  assert.equal(first.status, 0, first.stderr + first.stdout);

  const second = runScript(['--config', configPath], { env });
  assert.equal(second.status, 0, second.stderr + second.stdout);
  assert.match(second.stdout, /resuming idempotently/);

  const registry = readProjectsConfig(projectsConfigPath);
  const matches = registry.projects.filter((p) => p.name === 'acceptance-project');
  assert.equal(matches.length, 1, 'retrying must not register a duplicate project');
  void projectsDir;
});

test('resuming an initialized project with different decisions is refused before further mutation', () => {
  // The shipped catalog has exactly one (type, stack) pair today, so two
  // independently-valid --config files can never disagree on type/stack
  // for the same name. This pre-seeds the identity marker a prior run
  // would have written under a different decision (e.g. before a catalog
  // change, or hand-edited) to exercise the real refusal code path in
  // scripts/init-project.sh against a genuine mismatch.
  const { root, projectsDir, env } = makeIsolatedEnv();
  const configPath = writeConfigFile(root, 'config.json', VALID_CONFIG);

  const projectDir = path.join(projectsDir, 'acceptance-project');
  fs.mkdirSync(path.join(projectDir, 'src'), { recursive: true });
  fs.writeFileSync(
    path.join(projectDir, '.aigang-config-identity.json'),
    JSON.stringify({ schemaVersion: 1, name: 'acceptance-project', type: 'web', stack: 'some-other-stack' })
  );
  const canary = path.join(projectDir, 'src', 'canary.txt');
  fs.writeFileSync(canary, 'pre-existing, must not be touched by a refused run');

  const result = runScript(['--config', configPath], { env });

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /different configuration/);
  assert.equal(fs.readFileSync(canary, 'utf8'), 'pre-existing, must not be touched by a refused run');
});

// ---- judgment for unanticipated problems ----

// Builds a curated PATH directory containing a symlink to every command
// found on the real PATH except `excludeName`, so a test can remove
// exactly one dependency (leaving bash/node/coreutils otherwise intact)
// rather than filtering out whole directories, which on a system where
// unrelated tools share a directory with the excluded command (e.g.
// jq and bash both under /usr/bin) would remove far more than intended.
function pathWithoutCommand(originalPath, excludeName) {
  const curatedDir = fs.mkdtempSync(path.join(os.tmpdir(), `aigang-path-no-${excludeName}-`));
  const seen = new Set();
  for (const dir of (originalPath || '').split(path.delimiter)) {
    let entries;
    try {
      entries = fs.readdirSync(dir);
    } catch {
      continue;
    }
    for (const entry of entries) {
      if (entry === excludeName || seen.has(entry)) continue;
      const src = path.join(dir, entry);
      try {
        fs.symlinkSync(src, path.join(curatedDir, entry));
        seen.add(entry);
      } catch {
        // duplicate/broken symlink target — skip, first match wins like PATH
      }
    }
  }
  return curatedDir;
}

test('a missing dependency this path needs is reported with a clear diagnostic before any mutation', () => {
  const { root, projectsDir, env } = makeIsolatedEnv();
  const configPath = writeConfigFile(root, 'config.json', VALID_CONFIG);

  // Strip jq from PATH — the config-identity step depends on it —
  // so the failure exercised below is a genuine "command not found," not a
  // fabricated stub standing in for the real dependency check.
  const curatedPath = pathWithoutCommand(env.PATH, 'jq');

  const result = runScript(['--config', configPath], { env: { ...env, PATH: curatedPath } });

  assert.notEqual(result.status, 0);
  assert.match(result.stderr || '', /requires 'jq'/);
  assert.deepEqual(fs.readdirSync(projectsDir), []);
});
