'use strict';

const fs = require('fs');
const nodePath = require('path');
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

const TOP_LEVEL_FIELDS = new Set(['schemaVersion', 'project', 'repository']);
const PROJECT_FIELDS = new Set(['name', 'type', 'stack']);
const REPOSITORY_FIELDS = new Set(['url']);

// A repository URL is data, and it leaves this module as a plain
// `KEY=value` line on cli.js's stdout that a shell reads back one line at
// a time. A value carrying whitespace, a newline, or any other
// non-printable byte would break that framing, so the accepted set is
// restricted to printable, non-space ASCII rather than left for a caller
// to strip.
const URL_SAFE_PATTERN = /^[\x21-\x7e]+$/;

/**
 * The platform configuration's `repository.url` is the remote the project
 * is created against and pushed to, so it has to be a repository someone
 * can reach over the network — not a path on the machine that happens to
 * be running the flow, and not a credential. `file://`, `ssh://`,
 * `git@host:org/repo`, a bare path and a URL carrying `user:password` are
 * all refused here.
 *
 * A project configuration through `scripts/init-project.sh --config` is
 * not subject to this, the same exemption the template-placeholder check
 * takes: that path serves callers who legitimately point at a local
 * remote, and it never starts a platform.
 *
 * Returns a diagnostic, or null when the value is acceptable.
 */
function repositoryUrlError(url) {
  let parsed;
  try {
    parsed = new URL(url);
  } catch {
    return '"repository.url" must be an https:// URL of the project\'s repository '
      + `(got ${JSON.stringify(url)})`;
  }
  if (parsed.protocol !== 'https:') {
    return `"repository.url" must be an https:// URL, not ${JSON.stringify(parsed.protocol.replace(':', ''))}`;
  }
  if (parsed.username !== '' || parsed.password !== '') {
    return '"repository.url" must not carry credentials — the fine-grained PAT belongs in .env, as GH_TOKEN';
  }
  if (parsed.hostname === '') {
    return '"repository.url" must name a host';
  }
  if (parsed.pathname === '' || parsed.pathname === '/') {
    return '"repository.url" must name a repository, not just a host';
  }
  if (parsed.search !== '') {
    return '"repository.url" must not carry a query string';
  }
  if (parsed.hash !== '') {
    return '"repository.url" must not carry a fragment';
  }
  return null;
}

// The platform configuration template every operator copies to
// ai-gang.config.json. Its own field values ARE the placeholder set: a
// value still equal to one of them means that field was never filled in.
// Read from the shipped file rather than restated here, so the two can
// never drift apart.
const PLATFORM_TEMPLATE_FILENAME = 'ai-gang.config.template.json';
const PLATFORM_TEMPLATE_PATH = nodePath.join(
  __dirname, '..', '..', '..', '..', '..', PLATFORM_TEMPLATE_FILENAME
);

let templatePlaceholderCache = null;

/**
 * { 'project.name': '<template value>', ... } for every string-valued
 * field of the shipped template's `project` and `repository` objects.
 * Throws if the template is missing or unreadable — that is a broken
 * checkout, not an operator mistake, and the platform validator turns it
 * into its own diagnostic.
 */
function loadPlatformTemplatePlaceholders() {
  if (templatePlaceholderCache) return templatePlaceholderCache;
  const doc = JSON.parse(readUtf8Strict(PLATFORM_TEMPLATE_PATH));
  const placeholders = new Map();
  for (const objectName of ['project', 'repository']) {
    const obj = doc[objectName];
    if (!obj || typeof obj !== 'object' || Array.isArray(obj)) continue;
    for (const [key, value] of Object.entries(obj)) {
      if (typeof value === 'string') placeholders.set(`${objectName}.${key}`, value);
    }
  }
  templatePlaceholderCache = placeholders;
  return placeholders;
}

// What to tell an operator who left a field at its placeholder. For the
// two catalog-backed fields that is the catalog's own current contents,
// so the message never has to be updated when the catalog grows.
function placeholderGuidance(fieldPath, doc) {
  if (fieldPath === 'project.type') {
    return ` (supported deployment targets: ${listSupportedTargets().join(', ')})`;
  }
  if (fieldPath === 'project.stack') {
    const type = doc && doc.project && doc.project.type;
    if (isSupportedTarget(type)) {
      return ` (supported stack profiles for target "${type}": ${listSupportedStacks(type).join(', ')})`;
    }
    const pairs = listSupportedTargets().map((t) => `${t}: ${listSupportedStacks(t).join(', ')}`);
    return ` (supported stack profiles — ${pairs.join('; ')})`;
  }
  if (fieldPath === 'repository.url') {
    return ' (the HTTPS URL of the empty GitHub repository created for this project)';
  }
  return '';
}

function readUtf8Strict(filePath) {
  const buf = fs.readFileSync(filePath);
  // `fatal: true` makes invalid UTF-8 throw instead of silently
  // substituting U+FFFD, which is what a plain
  // `fs.readFileSync(path, 'utf8')` would do.
  const decoder = new TextDecoder('utf-8', { fatal: true });
  return decoder.decode(buf);
}

/**
 * Validate a project configuration file on disk — the shape
 * `scripts/init-project.sh --config` accepts. Returns
 * { valid, errors, decisions } — `decisions` (an object with `name`,
 * `type`, `stack`) is present only when valid is true.
 */
function validateConfigFile(filePath) {
  return validateFile(filePath, { platform: false });
}

/**
 * Validate the platform configuration file on disk — `ai-gang.config.json`,
 * the operator's copy of `ai-gang.config.template.json` that the platform
 * startup flow reads before any service starts. Every project rule above
 * still applies, unchanged; on top of them this path requires the
 * `repository` object and rejects any field still at its template
 * placeholder. `decisions` additionally carries `repositoryUrl`.
 */
function validatePlatformConfigFile(filePath) {
  return validateFile(filePath, { platform: true });
}

function validateFile(filePath, options) {
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
  return options && options.platform ? validatePlatformConfigText(text) : validateConfigText(text);
}

// Validate already-read project configuration text. Not exported:
// `validateConfigFile` is the only way in, so there is one entry point for
// callers and for the tests — the same standard `validatePlatformConfigText`
// below is held to.
function validateConfigText(text) {
  return validateText(text, { platform: false });
}

// Validate already-read platform configuration text. Reads
// `ai-gang.config.template.json` for the placeholder set, and nothing
// else. Not exported: `validatePlatformConfigFile` is the only way in,
// so there is one entry point for callers and for the tests.
function validatePlatformConfigText(text) {
  return validateText(text, { platform: true });
}

function validateText(text, { platform } = { platform: false }) {
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

  // `repository` is required only by the platform configuration. A project
  // configuration may carry it or leave it out; when it is there, it is
  // held to the same shape either way, so one file can serve both paths.
  if (doc.repository === undefined) {
    if (platform) {
      errors.push('missing required field "repository"');
    }
  } else if (doc.repository === null || typeof doc.repository !== 'object' || Array.isArray(doc.repository)) {
    errors.push('"repository" must be an object');
  } else {
    for (const key of Object.keys(doc.repository)) {
      if (!REPOSITORY_FIELDS.has(key)) {
        errors.push(`unknown field "repository.${key}"`);
      }
    }
    const url = doc.repository.url;
    if (url === undefined) {
      errors.push('missing required field "repository.url"');
    } else if (typeof url !== 'string' || url.length === 0) {
      errors.push(`"repository.url" must be a nonempty string (got ${JSON.stringify(url)})`);
    }
  }

  if (errors.length > 0) {
    return { valid: false, errors };
  }

  // The placeholder check, platform configuration only. It runs before the
  // name/catalog rules below so an unedited template reports "you did not
  // fill this in", which is actionable, rather than "not a supported
  // deployment target", which is not.
  if (platform) {
    let placeholders;
    try {
      placeholders = loadPlatformTemplatePlaceholders();
    } catch (err) {
      return {
        valid: false,
        errors: [`cannot read ${PLATFORM_TEMPLATE_FILENAME} at ${PLATFORM_TEMPLATE_PATH}: ${err.message}`],
      };
    }
    for (const [fieldPath, placeholderValue] of placeholders) {
      const [objectName, key] = fieldPath.split('.');
      const container = doc[objectName];
      if (!container || typeof container !== 'object') continue;
      if (container[key] === placeholderValue) {
        errors.push(
          `"${fieldPath}" is still at its ${PLATFORM_TEMPLATE_FILENAME} placeholder ` +
            `${JSON.stringify(placeholderValue)} — fill it in${placeholderGuidance(fieldPath, doc)}`
        );
      }
    }
    if (errors.length > 0) {
      return { valid: false, errors };
    }
  }

  const { name, type, stack } = doc.project;

  if (doc.repository !== undefined && !URL_SAFE_PATTERN.test(doc.repository.url)) {
    errors.push(
      '"repository.url" must contain only printable, non-space ASCII characters'
    );
  } else if (platform && doc.repository !== undefined) {
    const urlError = repositoryUrlError(doc.repository.url);
    if (urlError) errors.push(urlError);
  }

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

  const decisions = { name, type, stack };
  if (doc.repository !== undefined) {
    decisions.repositoryUrl = doc.repository.url;
  }

  return { valid: true, errors: [], decisions };
}

module.exports = {
  validateConfigFile,
  validatePlatformConfigFile,
};
