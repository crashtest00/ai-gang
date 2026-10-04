'use strict';

// v5.1 BUGFIXES.md BF-04 row 69's drift guard — scoped to exactly what rows
// 18, 39, 62, 63, 66 and 67 needed, no wider.
//
// setup/DEVOPS_HANDBOOK_v1.md and setup/agents/*.md name agent roles in free
// text, and nothing before this checked those names against the catalog
// that defines them (services/scrummaster/config/agents.json) — unlike
// gate 4's two existing mechanisms, which do read config out of Source
// rather than restating it (startup-env.test.js parses derive-env.sh's
// arrays; dispatch-snapshot.test.js enumerates setup/ entries). Row 62
// found "The Engineering Lead agent" named as a role in both files, when
// agents.json has never declared one — decomposition is refinement-agent's.
// This is that row's own concrete suggestion: a test asserting that every
// role name appearing in this scope's prose resolves to an agents.json
// entry.
//
// This does NOT attempt the harder, unresolved half of row 69 — claims
// about pipeline *behaviour* (rows 63, 66 and 67's "no dev environment
// exists" class). The audit recorded that class as needing "a different
// anchor": a mechanical identity check cannot settle whether a sentence
// describing what a pipeline does is still true, only whether a name it
// uses still exists.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const REPO_ROOT = path.join(__dirname, '..', '..', '..', '..');
const AGENTS_JSON = path.join(REPO_ROOT, 'services', 'scrummaster', 'config', 'agents.json');
const HANDBOOK = path.join(REPO_ROOT, 'setup', 'DEVOPS_HANDBOOK_v1.md');
const AGENTS_DIR = path.join(REPO_ROOT, 'setup', 'agents');

// Leading filler words a sentence puts in front of a role name — "The
// Refinement Agent interprets...", "Each Dev Agent reads..." — stripped so
// what remains is just the name to check.
const LEADING_FILLER = /^(The|A|An|Each|That|This|Every|Any|Your)\s+/;

function knownDisplayNames() {
  const catalog = JSON.parse(fs.readFileSync(AGENTS_JSON, 'utf8'));
  const names = (catalog.agents || []).map((a) => a.displayName);
  const retired = (catalog.retiredAgents || []).map((a) => (typeof a === 'string' ? null : a.displayName)).filter(Boolean);
  return new Set([...names, ...retired]);
}

function scopeFiles() {
  const files = [HANDBOOK];
  for (const name of fs.readdirSync(AGENTS_DIR)) {
    if (name.endsWith('.md')) files.push(path.join(AGENTS_DIR, name));
  }
  return files;
}

// A capitalized role-shaped phrase ending in "Agent" — up to four leading
// capitalized words, so "The Engineering Lead Agent" is captured whole.
// Lower-case "agent" (generic: "a dev agent", "the agent", "each agent") is
// deliberately not matched — this scope's own convention capitalizes a
// specific role's name ("Refinement Agent", "DevOps Agent") and does not
// capitalize the generic noun.
const ROLE_PHRASE = /\b(?:[A-Z][a-zA-Z]*)(?:\s+[A-Z][a-zA-Z]*){0,3}\s+Agent\b/g;

test('every capitalized "<Role> Agent" phrase in DEVOPS_HANDBOOK_v1.md and setup/agents/*.md names a role the agent catalog still declares', () => {
  const known = knownDisplayNames();
  assert.ok(known.size > 0, 'the catalog itself must declare at least one agent');

  for (const file of scopeFiles()) {
    const text = fs.readFileSync(file, 'utf8');
    const matches = new Set(text.match(ROLE_PHRASE) || []);
    for (const phrase of matches) {
      const stripped = phrase.replace(LEADING_FILLER, '');
      const resolves = known.has(phrase) || known.has(stripped)
        || [...known].some((name) => phrase === name || phrase.endsWith(` ${name}`));
      assert.ok(
        resolves,
        `${path.relative(REPO_ROOT, file)} names "${phrase}", which is not a role services/scrummaster/config/agents.json declares (known: ${[...known].join(', ')})`
      );
    }
  }
});
