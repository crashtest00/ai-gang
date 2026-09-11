'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { parseDfUsedPercent, getUsedPercent } = require('../scripts/lib/disk-usage');

const SAMPLE_DF_OUTPUT = `Filesystem     1024-blocks     Used Available Capacity Mounted on
overlay          61255492 51823456   6297652      90% /var/jenkins_home
`;

test('parseDfUsedPercent: extracts the capacity column from real df -P output', () => {
  assert.equal(parseDfUsedPercent(SAMPLE_DF_OUTPUT), 90);
});

test('parseDfUsedPercent: throws on output with no data line', () => {
  assert.throws(() => parseDfUsedPercent('Filesystem 1024-blocks Used Available Capacity Mounted on\n'));
});

test('parseDfUsedPercent: throws when the capacity field is missing/malformed', () => {
  assert.throws(() => parseDfUsedPercent('a b c d e f\nfoo bar baz qux 12 quux\n'));
});

test('getUsedPercent: runs df via the injected execFn and parses its output', () => {
  let calledArgs;
  const fakeExec = (cmd, args) => {
    calledArgs = [cmd, ...args];
    return SAMPLE_DF_OUTPUT;
  };
  const percent = getUsedPercent('/var/jenkins_home', { execFn: fakeExec });
  assert.equal(percent, 90);
  assert.deepEqual(calledArgs, ['df', '-P', '/var/jenkins_home']);
});

// Real integration check: df is a near-universal POSIX tool, so exercise
// the actual binary against a real path (this test's own tmpdir) rather
// than only the injected-fake path above, to prove the real invocation
// works end to end on this platform.
test('getUsedPercent: real df against a real path returns a plausible percent', { skip: process.platform === 'win32' }, () => {
  const percent = getUsedPercent(require('node:os').tmpdir());
  assert.equal(typeof percent, 'number');
  assert.ok(percent >= 0 && percent <= 100, `expected 0-100, got ${percent}`);
});
