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

function coreComposeAlias() {
  const compose = read(path.join(REPO_ROOT, 'services', 'core', 'docker-compose.yml'));
  const match = compose.match(/aliases:\s*\n\s*-\s*(\S+)/);
  assert.ok(match, "expected an `aliases:` entry under the api service's network block in services/core/docker-compose.yml");
  return match[1];
}

test("the core compose file's network alias matches canonicalWorkItems.js's default WORKITEM_SERVICE_URL host", () => {
  const alias = coreComposeAlias();
  const canonical = read(path.join(REPO_ROOT, 'services', 'scrummaster', 'src', 'canonicalWorkItems.js'));
  const defaultMatch = canonical.match(/process\.env\.WORKITEM_SERVICE_URL \|\| 'http:\/\/([^:']+):(\d+)'/);
  assert.ok(defaultMatch, "expected canonicalWorkItems.js's baseUrl() to fall back to a literal http://<host>:<port>");
  assert.equal(defaultMatch[1], alias, "canonicalWorkItems.js's default host does not match services/core/docker-compose.yml's alias");
  assert.equal(defaultMatch[2], '9100');
});

test("the core compose file's network alias matches jenkins/docker-compose.yml's WORKITEM_SERVICE_URL host", () => {
  const alias = coreComposeAlias();
  const jenkinsCompose = read(path.join(REPO_ROOT, 'jenkins', 'docker-compose.yml'));
  const jenkinsMatch = jenkinsCompose.match(/WORKITEM_SERVICE_URL=http:\/\/([^:]+):(\d+)/);
  assert.ok(jenkinsMatch, 'expected jenkins/docker-compose.yml to set WORKITEM_SERVICE_URL=http://<host>:<port>');
  assert.equal(jenkinsMatch[1], alias, "jenkins/docker-compose.yml's WORKITEM_SERVICE_URL host does not match services/core/docker-compose.yml's alias");
  assert.equal(jenkinsMatch[2], '9100');
});
