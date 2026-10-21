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

// A capitalized role-shaped phrase ending in "Agent" or "agent" — up to four
// leading capitalized words, so "The Engineering Lead Agent" and "The
// Engineering Lead agent" are both captured whole. The lower-case form
// matters: BF-04 row 62's drift was "The Engineering Lead agent interprets",
// which a capital-only "Agent" never saw (v5.2 audit row 31). A phrase whose
// only capitalized word is leading filler ("Each agent", "The agent", "An
// agent") is the generic noun and is not a role name — see isGenericPhrase.
// A lower-case qualifier ("a dev agent") is still not matched: it is not
// shaped like a role name.
// Horizontal whitespace only: a phrase never spans a line break (a heading
// "Assign Work To" above a paragraph "The agent" is not one role phrase).
const ROLE_PHRASE = /\b(?:[A-Z][a-zA-Z]*)(?:[ \t]+[A-Z][a-zA-Z]*){0,3}[ \t]+[Aa]gent\b/g;

// Case-insensitive on purpose: "Refinement agent" names the same role as
// "Refinement Agent".
function isGenericPhrase(phrase) {
  const stripped = phrase.replace(LEADING_FILLER, '');
  return /^agent$/i.test(stripped);
}

// The phrases in `text` that look like a role name and that no catalog
// display name accounts for. `known` is a Set of agents.json displayNames.
function findUnresolvedRoles(text, known) {
  const knownLower = new Set([...known].map((n) => n.toLowerCase()));
  const unresolved = [];
  for (const phrase of new Set(text.match(ROLE_PHRASE) || [])) {
    if (isGenericPhrase(phrase)) continue;
    const candidates = [phrase, phrase.replace(LEADING_FILLER, '')].map((c) => c.toLowerCase());
    const resolves = candidates.some((c) => knownLower.has(c))
      || [...knownLower].some((name) => candidates[0].endsWith(` ${name}`));
    if (!resolves) unresolved.push(phrase);
  }
  return unresolved;
}

test('every capitalized "<Role> Agent" phrase in DEVOPS_HANDBOOK_v1.md and setup/agents/*.md names a role the agent catalog still declares', () => {
  const known = knownDisplayNames();
  assert.ok(known.size > 0, 'the catalog itself must declare at least one agent');

  for (const file of scopeFiles()) {
    const text = fs.readFileSync(file, 'utf8');
    const unresolved = findUnresolvedRoles(text, known);
    assert.deepEqual(
      unresolved,
      [],
      `${path.relative(REPO_ROOT, file)} names ${unresolved.map((p) => `"${p}"`).join(', ')}, which is not a role services/scrummaster/config/agents.json declares (known: ${[...known].join(', ')})`
    );
  }
});

test('the guard flags the drift it was written for: "The Engineering Lead agent" is unresolved against the catalog', () => {
  const known = knownDisplayNames();
  assert.deepEqual(
    findUnresolvedRoles('The Engineering Lead agent interprets the request.', known),
    ['The Engineering Lead agent']
  );
  // The capital-"Agent" spelling is flagged too.
  assert.deepEqual(
    findUnresolvedRoles('The Engineering Lead Agent interprets the request.', known),
    ['The Engineering Lead Agent']
  );
});

test('the guard passes a correct display name and generic filler plus "agent"', () => {
  const known = knownDisplayNames();
  const [displayName] = [...known];
  assert.deepEqual(findUnresolvedRoles(`The ${displayName} reads the issue.`, known), []);
  assert.deepEqual(findUnresolvedRoles(`Each ${displayName.toLowerCase()} reads the issue.`, known), []);
  for (const generic of ['Each agent', 'The agent', 'An agent', 'Any agent', 'This agent', 'Every agent', 'Your agent']) {
    assert.deepEqual(findUnresolvedRoles(`${generic} reads the issue.`, known), [], generic);
  }
});
