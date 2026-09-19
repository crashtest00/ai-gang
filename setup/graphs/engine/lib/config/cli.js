#!/usr/bin/env node
'use strict';

// Config validation entrypoint for `scripts/init-project.sh --config
// <file>`. Deliberately thin: all real validation logic lives in
// validate.js/catalog.js so it can be unit-tested directly; this file only
// adapts that result to a process boundary bash can consume without
// `eval`-ing anything.
//
// On success: exits 0, prints normalized decisions to stdout as plain
// `KEY=value` lines (one per configured decision), each value already
// constrained to a safe character set by validate.js (the project-name
// pattern, or a fixed catalog identifier) before it ever reaches here.
// On failure: exits 1, prints one "config error: ..." diagnostic per
// validation failure to stderr, and prints nothing to stdout.
//
// `--platform` selects the platform configuration rules instead: the
// `repository` object becomes required and every field is checked against
// ai-gang.config.template.json's own placeholder values. That mode also
// prints REPOSITORY_URL. It is what the platform startup steps under
// scripts/startup/ validate ai-gang.config.json with, before any service
// container exists.

const path = require('path');
const { validateConfigFile, validatePlatformConfigFile } = require('./validate');

function main(argv) {
  const args = argv.slice(2);
  let platform = false;
  const positional = [];
  for (const arg of args) {
    if (arg === '--platform') {
      platform = true;
    } else {
      positional.push(arg);
    }
  }

  const filePath = positional[0];
  if (!filePath || positional.length > 1) {
    process.stderr.write('usage: cli.js [--platform] <config-file>\n');
    return 2;
  }

  const resolved = path.resolve(filePath);
  const result = platform ? validatePlatformConfigFile(resolved) : validateConfigFile(resolved);

  if (!result.valid) {
    for (const err of result.errors) {
      process.stderr.write(`config error: ${err}\n`);
    }
    return 1;
  }

  const { name, type, stack, repositoryUrl } = result.decisions;
  process.stdout.write(`PROJECT_NAME=${name}\n`);
  process.stdout.write(`PROJECT_TYPE=${type}\n`);
  process.stdout.write(`PROJECT_STACK=${stack}\n`);
  if (repositoryUrl !== undefined) {
    process.stdout.write(`REPOSITORY_URL=${repositoryUrl}\n`);
  }
  return 0;
}

if (require.main === module) {
  process.exitCode = main(process.argv);
}

module.exports = { main };
