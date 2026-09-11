'use strict';

// Direct, in-process coverage of lib/config/cli.js's main() — the same
// function scripts/init-project.sh --config invokes as a subprocess (see
// config-init-cli.test.js for that real, end-to-end path). This file
// covers its stdout/stderr contract quickly without a subprocess per case.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { main } = require('../lib/config/cli');

function withCapturedOutput(fn) {
  const out = [];
  const err = [];
  const realOut = process.stdout.write.bind(process.stdout);
  const realErr = process.stderr.write.bind(process.stderr);
  process.stdout.write = (chunk, ...rest) => {
    out.push(String(chunk));
    return realOut ? true : true;
  };
  process.stderr.write = (chunk, ...rest) => {
    err.push(String(chunk));
    return true;
  };
  try {
    const code = fn();
    return { code, stdout: out.join(''), stderr: err.join('') };
  } finally {
    process.stdout.write = realOut;
    process.stderr.write = realErr;
  }
}

function writeTempConfig(content) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'aigang-config-cli-'));
  const filePath = path.join(dir, 'config.json');
  fs.writeFileSync(filePath, JSON.stringify(content));
  return filePath;
}

test('cli.main: prints KEY=value decisions and exits 0 for a valid config', () => {
  const configPath = writeTempConfig({
    schemaVersion: 1,
    project: { name: 'acceptance-project', type: 'web', stack: 'node-express' },
  });

  const { code, stdout, stderr } = withCapturedOutput(() => main(['node', 'cli.js', configPath]));

  assert.equal(code, 0);
  assert.equal(stderr, '');
  assert.equal(stdout, 'PROJECT_NAME=acceptance-project\nPROJECT_TYPE=web\nPROJECT_STACK=node-express\n');
});

test('cli.main: exits 1 and prints "config error:" diagnostics for an invalid config, with nothing on stdout', () => {
  const configPath = writeTempConfig({ schemaVersion: 1, project: { name: 'acceptance-project', type: 'web' } });

  const { code, stdout, stderr } = withCapturedOutput(() => main(['node', 'cli.js', configPath]));

  assert.equal(code, 1);
  assert.equal(stdout, '');
  assert.match(stderr, /^config error: /);
});

test('cli.main: exits 2 with a usage message when no file argument is given', () => {
  const { code, stdout, stderr } = withCapturedOutput(() => main(['node', 'cli.js']));

  assert.equal(code, 2);
  assert.equal(stdout, '');
  assert.match(stderr, /usage:/);
});
