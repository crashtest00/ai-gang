'use strict';

const fs = require('fs');
const { findDuplicateKeyPaths } = require('./duplicate-keys');
const {
  isSupportedTarget,
  isSupportedStack,
  listSupportedTargets,
  listSupportedStacks,
} = require('./catalog');

const SUPPORTED_SCHEMA_VERSION = 1;

// Mirrors scripts/init-project.sh's own interactive-flow project-name
// check (`grep -qE '^[a-z][a-z0-9-]+$'`) so a config-driven name is held to
// exactly the same safety rule as a typed-in one — one shared rule, not a
// second, possibly-looser one.
const PROJECT_NAME_PATTERN = /^[a-z][a-z0-9-]+$/;

const TOP_LEVEL_FIELDS = new Set(['schemaVersion', 'project']);
const PROJECT_FIELDS = new Set(['name', 'type', 'stack']);

function readUtf8Strict(filePath) {
  const buf = fs.readFileSync(filePath);
  // `fatal: true` makes invalid UTF-8 throw instead of silently
  // substituting U+FFFD, which is what a plain
  // `fs.readFileSync(path, 'utf8')` would do.
  const decoder = new TextDecoder('utf-8', { fatal: true });
  return decoder.decode(buf);
}

/**
 * Validate a config file on disk. Returns { valid, errors, decisions } —
 * `decisions` (an object with `name`, `type`, `stack`) is present only
 * when valid is true.
 */
function validateConfigFile(filePath) {
  let text;
  try {
    text = readUtf8Strict(filePath);
  } catch (err) {
    if (err && err.code === 'ENOENT') {
      return { valid: false, errors: [`config file not found: ${filePath}`] };
    }
    if (err && err.code === 'EISDIR') {
      return { valid: false, errors: [`config file is a directory, not a file: ${filePath}`] };
    }
    return { valid: false, errors: [`config file is not valid UTF-8: ${filePath}`] };
  }
  return validateConfigText(text);
}

/**
 * Validate already-read config text. Pure function, no file I/O — used
 * directly by validateConfigFile and independently unit-testable.
 */
function validateConfigText(text) {
  const errors = [];

  const dupes = findDuplicateKeyPaths(text);
  for (const path of dupes) {
    errors.push(`duplicate key "${path}" in config`);
  }

  let doc;
  try {
    doc = JSON.parse(text);
  } catch (err) {
    errors.push(`config is not valid JSON: ${err.message}`);
    return { valid: false, errors };
  }

  if (dupes.length > 0) {
    // A duplicate key is a hard failure on its own regardless of what
    // JSON.parse resolved it to (it silently keeps the last occurrence) —
    // don't also report follow-on shape errors derived from that
    // collapsed, ambiguous value.
    return { valid: false, errors };
  }

  if (doc === null || typeof doc !== 'object' || Array.isArray(doc)) {
    errors.push('config root must be a JSON object');
    return { valid: false, errors };
  }

  for (const key of Object.keys(doc)) {
    if (!TOP_LEVEL_FIELDS.has(key)) {
      errors.push(`unknown field "${key}" at config root`);
    }
  }

  if (doc.schemaVersion === undefined) {
    errors.push('missing required field "schemaVersion"');
  } else if (doc.schemaVersion !== SUPPORTED_SCHEMA_VERSION) {
    errors.push(
      `unsupported schemaVersion ${JSON.stringify(doc.schemaVersion)} (only ${SUPPORTED_SCHEMA_VERSION} is supported)`
    );
  }

  if (doc.project === undefined) {
    errors.push('missing required field "project"');
  } else if (doc.project === null || typeof doc.project !== 'object' || Array.isArray(doc.project)) {
    errors.push('"project" must be an object');
  } else {
    for (const key of Object.keys(doc.project)) {
      if (!PROJECT_FIELDS.has(key)) {
        errors.push(`unknown field "project.${key}"`);
      }
    }
    for (const field of PROJECT_FIELDS) {
      const value = doc.project[field];
      if (value === undefined) {
        errors.push(`missing required field "project.${field}"`);
      } else if (typeof value !== 'string' || value.length === 0) {
        errors.push(`"project.${field}" must be a nonempty string (got ${JSON.stringify(value)})`);
      }
    }
  }

  if (errors.length > 0) {
    return { valid: false, errors };
  }

  const { name, type, stack } = doc.project;

  if (!PROJECT_NAME_PATTERN.test(name)) {
    errors.push(
      `"project.name" "${name}" is not a safe project name (must start with a lowercase letter and contain only lowercase letters, numbers, and hyphens)`
    );
  }

  if (!isSupportedTarget(type)) {
    errors.push(
      `"project.type" "${type}" is not a supported deployment target (supported: ${listSupportedTargets().join(', ')})`
    );
  } else if (!isSupportedStack(type, stack)) {
    errors.push(
      `"project.stack" "${stack}" is not a supported stack profile for target "${type}" (supported: ${listSupportedStacks(type).join(', ')})`
    );
  }

  if (errors.length > 0) {
    return { valid: false, errors };
  }

  return { valid: true, errors: [], decisions: { name, type, stack } };
}

module.exports = {
  validateConfigFile,
  validateConfigText,
};
