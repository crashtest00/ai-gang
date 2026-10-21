'use strict';

// docs/ClaudeInstructions.md and deployment-target-boilerplate.graph.yaml
// both tell a reader how to find the project-local config identity file
// scripts/init-project.sh --config writes (.aigang-config-identity.json)
// and which of its fields to read (`type`). Both are free text, so nothing
// stops them from drifting out of sync with what the script actually
// writes if the script's filename or field names ever change.
//
// This drives the real --config entrypoint (same isolated-environment
// approach as config-init-cli.test.js) to find out what it actually
// writes, then checks the doc and the graph against that ground truth
// instead of against each other.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const REPO_ROOT = path.join(__dirname, '..', '..', '..', '..');
const SCRIPT_PATH = path.join(REPO_ROOT, 'scripts', 'init-project.sh');
const DOC_PATH = path.join(REPO_ROOT, 'docs', 'ClaudeInstructions.md');
const GRAPH_PATH = path.join(REPO_ROOT, 'setup', 'graphs', 'deployment-target-boilerplate.graph.yaml');

const VALID_CONFIG = {
  schemaVersion: 2,
  project: { name: 'acceptance-project', type: 'web', stack: 'node-express' },
};

// init-project.sh takes the authentication method from the PLATFORM
// configuration in every mode, never from the project's --config file, and
// refuses to start without a valid one. AIGANG_CONFIG_FILE is the override
// platform startup itself uses to name it.
function writePlatformConfig(root, authMethod = 'api-key') {
  const file = path.join(root, 'platform.config.json');
  fs.writeFileSync(file, JSON.stringify({
    schemaVersion: 2,
    authMethod,
    project: { name: 'platform-project', type: 'web', stack: 'node-express' },
    repository: { url: 'https://github.com/an-org/a-repo.git' },
  }));
  return file;
}

// scripts/init-project.sh checks for Memory's shared harness volume ahead of
// every prompt, and creates the project's subdirectory of it, both through
// `docker` (Durable Agent Session Record REQ-01). This stand-in answers both
// without a daemon or the volume: `docker volume inspect` succeeds unless
// AIGANG_TEST_NO_HARNESS_VOLUME is set, `docker run` succeeds, and every
// call is logged to AIGANG_TEST_DOCKER_ARGV.
function stubDockerForHarness(root) {
  const bin = path.join(root, 'docker-bin');
  fs.mkdirSync(bin, { recursive: true });
  const stub = path.join(bin, 'docker');
  fs.writeFileSync(stub, [
    '#!/usr/bin/env bash',
    'printf "%s\\n" "$*" >> "$AIGANG_TEST_DOCKER_ARGV"',
    'if [[ "$1" == "volume" && "$2" == "inspect" ]]; then',
    '  [[ -n "${AIGANG_TEST_NO_HARNESS_VOLUME:-}" ]] && exit 1',
    '  exit 0',
    'fi',
    'exit 0',
    '',
  ].join('\n'));
  fs.chmodSync(stub, 0o755);
  return { PATH: `${bin}${path.delimiter}${process.env.PATH}`, AIGANG_TEST_DOCKER_ARGV: path.join(root, 'docker-argv.log') };
}

function makeIsolatedEnv() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'aigang-config-identity-refs-'));
  const projectsDir = path.join(root, 'projects');
  fs.mkdirSync(projectsDir, { recursive: true });
  const projectsConfigPath = path.join(root, 'projects.json');
  fs.writeFileSync(projectsConfigPath, JSON.stringify({ projects: [] }, null, 2));
  const env = {
    ...process.env,
    ...stubDockerForHarness(root),
    AIGANG_PROJECTS_DIR: projectsDir,
    AIGANG_PROJECTS_CONFIG: projectsConfigPath,
    HQ_ENV: path.join(root, 'nonexistent.env'),
    AIGANG_CONFIG_FILE: writePlatformConfig(root),
  };
  return { root, projectsDir, env };
}

// Finds every `` `<name>` field `` reference within a bounded window after
// the identity filename's first mention in the given text, so it ties the
// reference to that filename without getting tripped up by unrelated
// backtick-quoted tokens (e.g. a command name) in between. Returns the
// referenced field names, in the order they appear.
function extractReferencedIdentityFields(text, filename) {
  const idx = text.indexOf(filename);
  if (idx === -1) return [];
  const window = text.slice(idx, idx + filename.length + 400);
  const pattern = /`(\w+)`\s*field/g;
  const fields = [];
  let m;
  while ((m = pattern.exec(window)) !== null) {
    fields.push(m[1]);
  }
  return fields;
}

test('the identity filename and field names docs/ClaudeInstructions.md and the deployment-target graph refer to match what scripts/init-project.sh --config actually writes', () => {
  const { root, projectsDir, env } = makeIsolatedEnv();
  const configPath = path.join(root, 'config.json');
  fs.writeFileSync(configPath, JSON.stringify(VALID_CONFIG));

  const result = spawnSync('bash', [SCRIPT_PATH, '--config', configPath], {
    cwd: REPO_ROOT,
    env,
    input: '\ny\n',
    encoding: 'utf8',
    timeout: 15000,
  });
  assert.equal(result.status, 0, result.stderr + result.stdout);

  const projectDir = path.join(projectsDir, 'acceptance-project');
  const entries = fs.readdirSync(projectDir).filter((name) => name.startsWith('.aigang-config-identity'));
  assert.equal(entries.length, 1, `expected exactly one config-identity file, found: ${entries.join(', ')}`);
  const actualFilename = entries[0];

  const identity = JSON.parse(fs.readFileSync(path.join(projectDir, actualFilename), 'utf8'));
  const actualFields = new Set(Object.keys(identity));

  const docText = fs.readFileSync(DOC_PATH, 'utf8');
  const graphText = fs.readFileSync(GRAPH_PATH, 'utf8');

  assert.ok(
    docText.includes(actualFilename),
    `docs/ClaudeInstructions.md must reference the real identity filename "${actualFilename}"`
  );
  assert.ok(
    graphText.includes(actualFilename),
    `deployment-target-boilerplate.graph.yaml must reference the real identity filename "${actualFilename}"`
  );

  const docFields = extractReferencedIdentityFields(docText, actualFilename);
  const graphFields = extractReferencedIdentityFields(graphText, actualFilename);

  assert.ok(
    docFields.length > 0,
    'docs/ClaudeInstructions.md should name at least one identity-file field next to the filename — update this test\'s phrasing match if the doc\'s wording changed'
  );
  assert.ok(
    graphFields.length > 0,
    'the graph\'s probe should name at least one identity-file field next to the filename — update this test\'s phrasing match if the graph\'s wording changed'
  );

  for (const field of docFields) {
    assert.ok(
      actualFields.has(field),
      `docs/ClaudeInstructions.md refers to identity field "${field}", but scripts/init-project.sh only writes: ${[...actualFields].join(', ')}`
    );
  }
  for (const field of graphFields) {
    assert.ok(
      actualFields.has(field),
      `deployment-target-boilerplate.graph.yaml refers to identity field "${field}", but scripts/init-project.sh only writes: ${[...actualFields].join(', ')}`
    );
  }

  // The graph resolves the deployment target from the identity file, so
  // it must specifically be reading the field that holds the target.
  assert.ok(
    graphFields.includes('type'),
    'the graph resolves the deployment target from the identity file, so it must read its "type" field'
  );
});
