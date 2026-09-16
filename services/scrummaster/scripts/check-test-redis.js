#!/usr/bin/env node
'use strict';

// Fail fast, saying what is missing, when the Redis this package's
// Redis-backed test files need is not listening.
//
// Without this a bare `npm test` looks like it is working and then simply
// sits there: the Redis client retries a refused connection rather than
// failing, so the first such file hangs until the whole run is killed, with
// nothing on screen explaining why.
//
// A TCP connect and nothing more. It deliberately starts nothing: which
// containers run, and when, is the caller's decision, and a test command that
// quietly brings infrastructure up on its own is a worse surprise than one
// that stops and names what it needs.

const net = require('node:net');

const CONNECT_TIMEOUT_MS = 1500;

// The two ways the test files resolve where Redis is — test/streams.test.js
// and test/idempotency.test.js read REDIS_TEST_URL, the integration tests
// read REDIS_TEST_HOST/REDIS_TEST_PORT — so that whatever a caller has set,
// this checks the address the tests will actually dial.
function targets() {
  const found = new Map();

  const url = process.env.REDIS_TEST_URL;
  if (url) {
    try {
      const parsed = new URL(url);
      found.set(`${parsed.hostname}:${parsed.port || 6379}`, {
        host: parsed.hostname, port: Number(parsed.port) || 6379,
      });
    } catch {
      console.error(`REDIS_TEST_URL is not a URL: ${url}`);
      process.exit(1);
    }
  } else {
    found.set('localhost:16399', { host: 'localhost', port: 16399 });
  }

  const host = process.env.REDIS_TEST_HOST || 'localhost';
  const port = Number(process.env.REDIS_TEST_PORT) || 16399;
  found.set(`${host}:${port}`, { host, port });

  return [...found.values()];
}

function canConnect({ host, port }) {
  return new Promise(resolve => {
    const socket = net.createConnection({ host, port });
    const done = result => {
      socket.destroy();
      resolve(result);
    };
    socket.setTimeout(CONNECT_TIMEOUT_MS);
    socket.once('connect', () => done(true));
    socket.once('timeout', () => done(false));
    socket.once('error', () => done(false));
  });
}

async function main() {
  const unreachable = [];
  for (const target of targets()) {
    if (!(await canConnect(target))) unreachable.push(`${target.host}:${target.port}`);
  }
  if (unreachable.length === 0) return;

  console.error(
    `\nSeveral of this package's test files talk to a real Redis, and nothing is listening on ` +
    `${unreachable.join(', ')}.\n\n` +
    `Start the one the tests expect, from the repository root:\n\n` +
    `  docker compose -f services/work-item-service/docker-compose.test.yml up --wait\n\n` +
    `and stop it again when you are done:\n\n` +
    `  docker compose -f services/work-item-service/docker-compose.test.yml down -v\n\n` +
    `To run a test file that needs no Redis without this check, add --ignore-scripts.\n`
  );
  process.exit(1);
}

main();
