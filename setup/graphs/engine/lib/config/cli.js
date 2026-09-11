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

const path = require('path');
const { validateConfigFile } = require('./validate');

function main(argv) {
  const filePath = argv[2];
  if (!filePath) {
    process.stderr.write('usage: cli.js <config-file>\n');
    return 2;
  }

  const resolved = path.resolve(filePath);
  const result = validateConfigFile(resolved);

  if (!result.valid) {
    for (const err of result.errors) {
      process.stderr.write(`config error: ${err}\n`);
    }
    return 1;
  }

  const { name, type, stack } = result.decisions;
  process.stdout.write(`PROJECT_NAME=${name}\n`);
  process.stdout.write(`PROJECT_TYPE=${type}\n`);
  process.stdout.write(`PROJECT_STACK=${stack}\n`);
  return 0;
}

if (require.main === module) {
  process.exitCode = main(process.argv);
}

module.exports = { main };
