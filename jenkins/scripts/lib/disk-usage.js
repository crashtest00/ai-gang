'use strict';

// Reads current disk usage percent for a mount point via `df`, and parses
// its output. Parsing is split out as a pure function so it's unit
// testable against captured `df` output without actually running `df`.

const { execFileSync } = require('node:child_process');

/**
 * Parse `df -P <path>` output (POSIX format, stable across platforms) into
 * a used-percent number.
 *
 * @param {string} dfOutput
 * @returns {number} 0-100
 */
function parseDfUsedPercent(dfOutput) {
  const lines = dfOutput.trim().split('\n');
  if (lines.length < 2) {
    throw new Error(`parseDfUsedPercent: unexpected df output, no data line:\n${dfOutput}`);
  }
  // Filesystem 1024-blocks Used Available Capacity Mounted-on
  const dataLine = lines[lines.length - 1];
  const fields = dataLine.trim().split(/\s+/);
  const capacityField = fields[4];
  if (!capacityField || !capacityField.endsWith('%')) {
    throw new Error(`parseDfUsedPercent: could not find capacity field in: ${dataLine}`);
  }
  return parseInt(capacityField.replace('%', ''), 10);
}

/**
 * @param {string} mountPath
 * @param {object} [opts]
 * @param {typeof execFileSync} [opts.execFn] - injectable for tests
 * @returns {number} used percent, 0-100
 */
function getUsedPercent(mountPath, opts = {}) {
  const execFn = opts.execFn ?? execFileSync;
  const output = execFn('df', ['-P', mountPath], { encoding: 'utf8' });
  return parseDfUsedPercent(output);
}

module.exports = { parseDfUsedPercent, getUsedPercent };
