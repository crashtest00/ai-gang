'use strict';

// `JSON.parse` silently keeps the last occurrence of a repeated object key
// and gives no way to observe that a duplicate ever existed — but a
// duplicate key in a config file must be rejected outright, not quietly
// resolved to whichever copy happened to parse last. This module tokenizes
// the raw config text itself (rather than trusting JSON.parse's already-
// collapsed result) and reports every duplicate key path it finds, at any
// nesting depth, keyed independently per object (so two sibling objects
// may each safely use the same key name).
//
// This is a minimal tokenizer sufficient for well-formed JSON; malformed
// JSON is left for JSON.parse's own error to report.

function tokenize(text) {
  const tokens = [];
  let i = 0;
  const n = text.length;
  const isWs = (c) => c === ' ' || c === '\t' || c === '\n' || c === '\r';

  while (i < n) {
    const c = text[i];
    if (isWs(c)) {
      i++;
      continue;
    }
    if (c === '{' || c === '}' || c === '[' || c === ']' || c === ':' || c === ',') {
      tokens.push({ type: c });
      i++;
      continue;
    }
    if (c === '"') {
      let j = i + 1;
      while (j < n) {
        const ch = text[j];
        if (ch === '\\') {
          j += 2;
          continue;
        }
        if (ch === '"') break;
        j++;
      }
      if (j >= n) {
        throw new Error('unterminated string literal');
      }
      tokens.push({ type: 'string', raw: text.slice(i, j + 1) });
      i = j + 1;
      continue;
    }
    // number / true / false / null — consume until the next structural
    // character or whitespace; JSON.parse validates the actual shape.
    let j = i;
    while (j < n && !'{}[]:,'.includes(text[j]) && !isWs(text[j])) j++;
    if (j === i) {
      i++;
      continue;
    }
    tokens.push({ type: 'literal', raw: text.slice(i, j) });
    i = j;
  }
  return tokens;
}

/**
 * Return the dotted paths (e.g. "project.name") of every object key that
 * appears more than once within the same object in the given raw JSON
 * text. A key repeated N times is reported N-1 times (once per repeat).
 */
function findDuplicateKeyPaths(text) {
  let tokens;
  try {
    tokens = tokenize(text);
  } catch {
    return []; // malformed input — JSON.parse will report it
  }

  const duplicates = [];
  const stack = []; // { kind: 'object'|'array', seen?: Set<string>, label: string|null }
  let expectKey = false;
  let pendingLabel = null;

  for (const t of tokens) {
    if (t.type === '{') {
      stack.push({ kind: 'object', seen: new Set(), label: pendingLabel });
      pendingLabel = null;
      expectKey = true;
      continue;
    }
    if (t.type === '[') {
      stack.push({ kind: 'array', label: pendingLabel });
      pendingLabel = null;
      expectKey = false;
      continue;
    }
    if (t.type === '}' || t.type === ']') {
      stack.pop();
      continue;
    }
    if (t.type === ',') {
      const top = stack[stack.length - 1];
      if (top && top.kind === 'object') expectKey = true;
      continue;
    }
    if (t.type === ':') {
      expectKey = false;
      continue;
    }
    if (t.type === 'string') {
      const top = stack[stack.length - 1];
      if (top && top.kind === 'object' && expectKey) {
        let key;
        try {
          key = JSON.parse(t.raw);
        } catch {
          key = t.raw;
        }
        if (top.seen.has(key)) {
          const labels = stack.map((f) => f.label).filter((l) => l !== null);
          const path = [...labels, key].join('.');
          duplicates.push(path);
        }
        top.seen.add(key);
        pendingLabel = key;
      }
      continue;
    }
  }

  return duplicates;
}

module.exports = { findDuplicateKeyPaths };
