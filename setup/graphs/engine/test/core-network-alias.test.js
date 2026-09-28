'use strict';

// The network alias services/core/docker-compose.yml gives its `api`
// service (`core`) is the whole of core-service-rename.md REQ-02's host
// clause, and nothing static proves it: `git grep -F 'core:9100' -- '*test*'`
// is empty at the pinned commit, so deleting or renaming the alias leaves
// every suite green while breaking canonicalWorkItems.js's default and
// Jenkins's release-writeback target — both of which depend on it by
// address, not by any check that runs.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const REPO_ROOT = path.join(__dirname, '..', '..', '..', '..');

function read(file) {
  return fs.readFileSync(file, 'utf8');
}

// The literal core-service-rename.md REQ-02 names as the runtime host:
// `core:9100`. Pinned as a value here, not only as an agreement between the
// three sites — renaming it in all three at once would otherwise still pass.
const REQUIRED_ALIAS = 'core';
const REQUIRED_PORT = '9100';

// The alias under the `api` service specifically. Anchored at `  api:` and
// stopped at the next top-level service key, because a first `aliases:` block
// added to an earlier service — `core-postgres` or `migrate` — would silently
// retarget an unanchored match and leave `api`'s own alias unchecked (V5.0
// audit row 88).
function coreComposeAlias() {
  const compose = read(path.join(REPO_ROOT, 'services', 'core', 'docker-compose.yml'));
  const apiBlock = compose.match(/\n {2}api:\n([\s\S]*?)(?=\n {2}\S|\n\S|$)/);
  assert.ok(apiBlock, 'expected an `api:` service block in services/core/docker-compose.yml');
  const match = apiBlock[1].match(/aliases:\s*\n\s*-\s*(\S+)/);
  assert.ok(match, "expected an `aliases:` entry under the api service's network block in services/core/docker-compose.yml");
  return match[1];
}

test("the api service's network alias is the literal `core` REQ-02 names", () => {
  assert.equal(
    coreComposeAlias(),
    REQUIRED_ALIAS,
    `core-service-rename.md REQ-02 makes the runtime host ${REQUIRED_ALIAS}:${REQUIRED_PORT}; ` +
    'renaming this alias is a rename of the service\'s runtime host and needs that requirement changed first'
  );
});

test("the api service's published port is the one the alias is addressed on", () => {
  const compose = read(path.join(REPO_ROOT, 'services', 'core', 'docker-compose.yml'));
  const apiBlock = compose.match(/\n {2}api:\n([\s\S]*?)(?=\n {2}\S|\n\S|$)/);
  assert.ok(apiBlock);
  assert.match(apiBlock[1], new RegExp(`--bind 0\\.0\\.0\\.0:${REQUIRED_PORT}`));
});

test("the core compose file's network alias matches canonicalWorkItems.js's default WORKITEM_SERVICE_URL host", () => {
  const alias = coreComposeAlias();
  const canonical = read(path.join(REPO_ROOT, 'services', 'scrummaster', 'src', 'canonicalWorkItems.js'));
  const defaultMatch = canonical.match(/process\.env\.WORKITEM_SERVICE_URL \|\| 'http:\/\/([^:']+):(\d+)'/);
  assert.ok(defaultMatch, "expected canonicalWorkItems.js's baseUrl() to fall back to a literal http://<host>:<port>");
  assert.equal(defaultMatch[1], alias, "canonicalWorkItems.js's default host does not match services/core/docker-compose.yml's alias");
  assert.equal(defaultMatch[2], REQUIRED_PORT);
});

test("the core compose file's network alias matches jenkins/docker-compose.yml's WORKITEM_SERVICE_URL host", () => {
  const alias = coreComposeAlias();
  const jenkinsCompose = read(path.join(REPO_ROOT, 'jenkins', 'docker-compose.yml'));
  const jenkinsMatch = jenkinsCompose.match(/WORKITEM_SERVICE_URL=http:\/\/([^:]+):(\d+)/);
  assert.ok(jenkinsMatch, 'expected jenkins/docker-compose.yml to set WORKITEM_SERVICE_URL=http://<host>:<port>');
  assert.equal(jenkinsMatch[1], alias, "jenkins/docker-compose.yml's WORKITEM_SERVICE_URL host does not match services/core/docker-compose.yml's alias");
  assert.equal(jenkinsMatch[2], REQUIRED_PORT);
});
