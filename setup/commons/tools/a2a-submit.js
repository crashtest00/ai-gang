#!/usr/bin/env node
'use strict';

/**
 * a2a-submit.js — the constructor entry point of AI Gang's gateway publish
 * tool (V5.0 Deterministic Gateway Message Tooling REQ-01, REQ-02, REQ-03).
 *
 * An agent names an operation and gives it its fields. Everything else comes
 * from the dispatch: the project name and the task this session is working,
 * from the environment the subscriber exports (Agent Commons REQ-03); the
 * message id, the `kind`, the `role`, the Part wrapping and the transport
 * envelope, from this tool; and the chain — which message this one replies to
 * — from per-session state this tool keeps beside the commons snapshot. No id
 * is ever passed in, printed for an agent to remember, or typed by one.
 *
 * The built message is validated by a2a-validate.js before any write to
 * Streams. A failure exits non-zero and names the failing field(s) on stderr,
 * in the same call the agent made, and publishes nothing.
 *
 *   a2a-submit.js help                  the operations and their arguments
 *   a2a-submit.js <operation> [flags]
 *
 * Reads: PROJECT_NAME, A2A_TASK_ID, A2A_CONTEXT_ID, A2A_LAST_MESSAGE_ID,
 * AIGANG_COMMONS_DIR (whose sibling `state/` directory holds the chain), and
 * REDIS_HOST/REDIS_PORT for the connection.
 */

const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const { parseArgs } = require('util');
const { createClient } = require('redis');
const { buildEnvelope, KIND, toStreamFields } = require('./envelope');
const { validateGatewayPayload, GatewayValidationError } = require('./a2a-validate');

const TOOL = 'a2a-submit.js';

// Redis is reached over the container network, and an unreachable one used to
// retry for ever: one ECONNREFUSED per attempt on stderr and no exit, bounded
// only by the subscriber's 30-minute session kill (V5.0 audit row 9). Three
// connection attempts, each itself bounded, then the strategy returns an Error
// — which makes `connect()` reject, so this tool exits non-zero with the reason
// on stderr within seconds. gateway-publish.js, the raw entry point, carries
// the same two options for the same reason; Jenkins runs that one.
const CONNECT_TIMEOUT_MS = 5000;
const CONNECT_ATTEMPTS = 3;

function boundedSocket(redisUrl) {
  return {
    connectTimeout: CONNECT_TIMEOUT_MS,
    reconnectStrategy: retries => (
      retries + 1 >= CONNECT_ATTEMPTS
        ? new Error(`${redisUrl} is unreachable — gave up after ${CONNECT_ATTEMPTS} connection attempts`)
        : Math.min(200 * 2 ** retries, 1000)
    ),
  };
}

// Flags shared by more than one operation, declared once so the same argument
// means the same thing everywhere it appears.
const TEXT = { text: { type: 'string' } };
const REFERENCE = {
  'reference-file': { type: 'string' },
  'reference-function': { type: 'string' },
};

// `--reference-function` is the context *inside* the file `--reference-file`
// names, so on its own it reaches nothing: `withReference` below drops it, and
// the agent is never told. Refused instead, the same way the other two
// paired-flag cases are (`create-subtask`'s two specification flags,
// `completed`'s two pull-request flags) — SKILL.md already says "only with
// --reference-file" and nothing enforced it (V5.0 audit row 49). Shared by the
// four operations that take a reference at all.
function checkReferencePair(args, problems) {
  if (args['reference-function'] !== undefined && args['reference-file'] === undefined) {
    problems.push('--reference-function names the context inside the file --reference-file names, which was not given');
  }
}

/**
 * The argument surface. One entry per operation an agent can submit, holding
 * the A2A `state` it submits under, which flags it takes, which of those are
 * required, and how the `data` Part is built from them.
 *
 * `reference` appears on exactly the paths the gateway consumes it on: the
 * `comment` operation, a working-state progress submission with no
 * `operation`, and the `input-required`/`auth-required` states. The other
 * operations have no such flag because a value there would reach nothing.
 */
const OPERATIONS = {
  comment: {
    summary: 'Post a comment on the work item and keep working.',
    state: 'working',
    flags: { ...TEXT, ...REFERENCE },
    required: ['text'],
    check: checkReferencePair,
    data: args => withReference({ operation: 'comment' }, args),
  },

  progress: {
    summary: 'Post a plain progress note — a comment with no operation attached.',
    state: 'working',
    flags: { ...TEXT, ...REFERENCE },
    required: ['text'],
    check: checkReferencePair,
    // Deliberately no `operation`: this is the gateway's plain-progress path.
    data: args => withReference({}, args),
  },

  'input-required': {
    summary: 'Stop and ask a human: you need a clarification only a person can give.',
    state: 'input-required',
    flags: { ...TEXT, ...REFERENCE },
    required: ['text'],
    check: checkReferencePair,
    data: args => withReference({}, args),
  },

  'auth-required': {
    summary: 'Stop and ask a human: you are missing a credential or an authorization.',
    state: 'auth-required',
    flags: { ...TEXT, ...REFERENCE },
    required: ['text'],
    check: checkReferencePair,
    data: args => withReference({}, args),
  },

  reassign: {
    summary: "Hand the work item's recorded owner to another agent this project permits.",
    state: 'working',
    flags: { ...TEXT, agent: { type: 'string' } },
    required: ['agent'],
    data: args => ({ operation: 'reassign', agentFieldValue: args.agent }),
  },

  'create-subtask': {
    summary: 'Request a new subtask under the work item you are working on.',
    state: 'working',
    flags: {
      ...TEXT,
      summary: { type: 'string' },
      description: { type: 'string' },
      agent: { type: 'string' },
      'specification-artifact': { type: 'string' },
      'specification-requirement': { type: 'string' },
      'artifact-link': { type: 'string', multiple: true },
    },
    required: ['summary', 'description', 'agent'],
    check(args, problems) {
      const artifact = args['specification-artifact'];
      const requirement = args['specification-requirement'];
      if ((artifact === undefined) !== (requirement === undefined)) {
        problems.push('--specification-artifact and --specification-requirement go together: give both or neither');
      }
    },
    data(args) {
      const data = {
        operation: 'create_subtask',
        summary: args.summary,
        description: args.description,
        agentFieldValue: args.agent,
      };
      if (args['specification-artifact'] !== undefined) {
        data.specificationLink = {
          artifactId: args['specification-artifact'],
          requirementId: args['specification-requirement'],
        };
      }
      if (args['artifact-link'] !== undefined) data.artifactLinks = args['artifact-link'];
      return data;
    },
  },

  completed: {
    summary: 'Your work on this task is done. Add --pull-request when you opened one.',
    state: 'completed',
    flags: {
      ...TEXT,
      'pull-request': { type: 'string' },
      'pull-request-summary': { type: 'string' },
    },
    required: ['text'],
    check(args, problems) {
      if (args['pull-request-summary'] !== undefined && args['pull-request'] === undefined) {
        problems.push('--pull-request-summary describes the pull request named by --pull-request, which was not given');
      }
    },
    // The one operation that carries an Artifact. `handleCompleted` finds it by
    // name, reads the url from its file Part and the summary from its text
    // Part.
    artifacts(args, { taskId }) {
      if (args['pull-request'] === undefined) return undefined;
      const summary = args['pull-request-summary'] !== undefined ? args['pull-request-summary'] : args.text;
      return [{
        kind: 'artifact',
        artifactId: crypto.randomUUID(),
        taskId,
        name: 'pull-request',
        parts: [
          { kind: 'file', file: { name: 'pull-request', mimeType: 'text/uri-list', uri: args['pull-request'] } },
          { kind: 'text', text: summary },
        ],
      }];
    },
  },

  failed: {
    summary: 'You cannot finish this task: something broke that you cannot get past.',
    state: 'failed',
    flags: { ...TEXT },
    required: ['text'],
  },

  canceled: {
    summary: 'This task should not be carried out after all.',
    state: 'canceled',
    flags: { ...TEXT },
    required: ['text'],
  },

  rejected: {
    summary: 'You are refusing this task — it is not work this role should do.',
    state: 'rejected',
    flags: { ...TEXT },
    required: ['text'],
  },
};

function withReference(data, args) {
  const file = args['reference-file'];
  if (file === undefined) return Object.keys(data).length ? data : null;
  const reference = { file };
  if (args['reference-function'] !== undefined) reference.function = args['reference-function'];
  return { ...data, reference };
}

// The flag name an argument-level message should show for a wire field, so an
// agent is told what to type rather than what the message ended up looking
// like.
const WIRE_FIELD = {
  text: 'the message\'s text Part',
  agent: 'data.agentFieldValue',
  summary: 'data.summary',
  description: 'data.description',
};

function usage(operationName) {
  const lines = [];
  const names = operationName ? [operationName] : Object.keys(OPERATIONS);

  if (!operationName) {
    lines.push(`Usage: ${TOOL} <operation> [arguments]`);
    lines.push('');
    lines.push('Submits one A2A message to the ScrumMaster gateway for the task this');
    lines.push('session was dispatched for. The task, the project, the ids and the chain');
    lines.push('are not arguments: this tool reads them from the dispatch.');
    lines.push('');
    lines.push(`  ${TOOL} help <operation>   the arguments for one operation`);
    lines.push('');
    lines.push('Operations:');
  }

  for (const name of names) {
    const op = OPERATIONS[name];
    lines.push('');
    lines.push(`  ${name}  (state: ${op.state})`);
    lines.push(`      ${op.summary}`);
    const required = op.required || [];
    for (const flag of Object.keys(op.flags)) {
      const kind = op.flags[flag].multiple ? ' (repeatable)' : '';
      const need = required.includes(flag) ? 'required' : 'optional';
      lines.push(`      --${flag} <value>${kind}  ${need}`);
    }
  }

  lines.push('');
  lines.push('Examples:');
  lines.push(`  ${TOOL} comment --text "Migration written, tests next" --reference-file db/migrate.sql`);
  lines.push(`  ${TOOL} create-subtask --summary "Frontend Agent: wire the form" \\`);
  lines.push('      --description "Bind the new endpoint to the signup form." --agent frontend-agent');
  lines.push(`  ${TOOL} completed --text "Done — PR open." --pull-request https://github.com/o/r/pull/7`);
  lines.push('');
  lines.push('A non-zero exit means nothing was published. The reason is on stderr.');
  return lines.join('\n');
}

function fail(lines) {
  for (const line of Array.isArray(lines) ? lines : [lines]) console.error(line);
  process.exit(1);
}

// The dispatch context, from the environment the subscriber exports. A missing
// value is a broken session rather than a mistake an agent made, so it is
// named as such.
function dispatchContext() {
  const missing = [];
  const read = name => {
    const value = process.env[name];
    if (!value) missing.push(name);
    return value;
  };
  const context = {
    project: (read('PROJECT_NAME') || '').toLowerCase(),
    taskId: read('A2A_TASK_ID'),
    contextId: read('A2A_CONTEXT_ID'),
    lastMessageId: read('A2A_LAST_MESSAGE_ID'),
    commonsDir: read('AIGANG_COMMONS_DIR'),
  };
  if (missing.length) {
    fail([
      `${TOOL}: this session is missing its dispatch context (${missing.join(', ')}).`,
      'This tool runs inside an agent session the AI Gang subscriber started, which exports it.',
      'Nothing was published.',
    ]);
  }
  return context;
}

// Per-session state, in the `state/` directory beside the commons snapshot:
// the snapshot itself is a content-hashed copy nothing writes into, and this
// state is deleted with the session (Agent Commons REQ-02).
function chainFile(commonsDir) {
  return path.join(path.dirname(commonsDir), 'state', 'a2a-chain.json');
}

function readChain(file) {
  try {
    const chain = JSON.parse(fs.readFileSync(file, 'utf8'));
    return chain && typeof chain === 'object' ? chain : {};
  } catch {
    return {};
  }
}

// Written only after the message is durably on the stream, and written whole:
// a half-written chain would send every later submission of this task to a
// referenceMessageId the server never accepted.
//
// Read-modify-write with an atomic rename but no lock, and deliberately so
// (V5.0 audit row 47). Two invocations racing here could lose the earlier
// entry, but nothing can put them in that position: the state directory is per
// dispatch, one dispatch works one task, and dispatches are serial (the
// subscriber's own runSerially queue). The worst a same-task race could do
// anyway is chain two submissions to one predecessor, and taskStore accepts
// that — they are siblings, not a rejection. A lock here would buy nothing and
// add a failure mode of its own.
function recordChain(file, taskId, messageId) {
  const chain = readChain(file);
  chain[taskId] = messageId;
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const temp = `${file}.${process.pid}.tmp`;
  fs.writeFileSync(temp, `${JSON.stringify(chain, null, 2)}\n`);
  fs.renameSync(temp, file);
}

function parseOperationArgs(operation, argv) {
  try {
    const { values, positionals } = parseArgs({
      args: argv,
      options: operation.flags,
      strict: true,
      allowPositionals: true,
    });
    if (positionals.length) {
      fail([
        `${TOOL}: unexpected argument "${positionals[0]}" — every value goes with a named flag.`,
        'Nothing was published.',
      ]);
    }
    return values;
  } catch (err) {
    fail([`${TOOL}: ${err.message}`, '', usage(process.argv[2]), '', 'Nothing was published.']);
  }
}

function checkRequired(name, operation, args) {
  const problems = [];
  for (const flag of operation.required || []) {
    if (args[flag] === undefined || args[flag] === '') {
      const wire = WIRE_FIELD[flag];
      problems.push(`--${flag} is required for "${name}"${wire ? ` — it becomes ${wire}` : ''}`);
    }
  }
  if (operation.check) operation.check(args, problems);
  if (problems.length) {
    fail([
      `${TOOL}: "${name}" is missing something, so nothing was published:`,
      ...problems.map(p => `  - ${p}`),
      '',
      `Run \`${TOOL} help ${name}\` for this operation's arguments.`,
    ]);
  }
}

function buildSubmission(name, operation, args, context) {
  const parts = [];
  if (args.text !== undefined) parts.push({ kind: 'text', text: args.text });
  const data = operation.data ? operation.data(args) : null;
  if (data) parts.push({ kind: 'data', data });

  const chain = chainFile(context.commonsDir);
  const previous = readChain(chain)[context.taskId];

  const message = {
    kind: 'message',
    messageId: crypto.randomUUID(),
    taskId: context.taskId,
    contextId: context.contextId,
    role: 'agent',
    referenceMessageId: previous || context.lastMessageId,
    parts,
  };

  const artifacts = operation.artifacts ? operation.artifacts(args, context) : undefined;
  return { state: operation.state, message, ...(artifacts ? { artifacts } : {}) };
}

async function main() {
  const [, , first, ...rest] = process.argv;

  if (!first || first === 'help' || first === '--help' || first === '-h') {
    const requested = rest[0] || null;
    if (requested && !OPERATIONS[requested]) {
      fail([`${TOOL}: there is no "${requested}" operation.`, '', usage()]);
    }
    console.log(usage(requested || null));
    return;
  }

  const operation = OPERATIONS[first];
  if (!operation) {
    fail([
      `${TOOL}: there is no "${first}" operation. The operations are: ${Object.keys(OPERATIONS).join(', ')}.`,
      '',
      'Nothing was published.',
    ]);
  }

  const args = parseOperationArgs(operation, rest);
  checkRequired(first, operation, args);

  const context = dispatchContext();
  const payload = buildSubmission(first, operation, args, context);

  try {
    validateGatewayPayload(payload);
  } catch (err) {
    if (!(err instanceof GatewayValidationError)) throw err;
    fail([
      `${TOOL}: the "${first}" submission this tool built is not valid, so nothing was published:`,
      ...err.errors.map(e => `  - ${e}`),
      '',
      `Run \`${TOOL} help ${first}\` for this operation's arguments.`,
    ]);
  }

  const stream = `aigang:gateway:${context.project}`;
  const envelope = buildEnvelope({
    kind: KIND.JIRA_OPERATION,
    project: context.project,
    taskId: context.taskId,
    contextId: context.contextId,
    payload,
  });

  const redisUrl = `redis://${process.env.REDIS_HOST || 'localhost'}:${process.env.REDIS_PORT || 6379}`;
  const client = createClient({ url: redisUrl, socket: boundedSocket(redisUrl) });
  client.on('error', err => console.error(`[${TOOL}] Redis error:`, err.message));

  try {
    await client.connect();
    const entryId = await client.xAdd(stream, '*', toStreamFields(envelope));
    // Only now is this message the one the next submission replies to. A
    // failure here has not lost the submission, and the exit status must say
    // so: this tool's own contract, in its usage text and in the a2a-submit
    // skill, is that a non-zero exit means nothing was published and the call
    // should be made again — and a re-run of `create-subtask` materialises a
    // second subtask, because handleCreateSubtask's idempotency key is the
    // envelope id, fresh on the re-run (V5.0 audit row 35). So this exits 0
    // and warns on stderr instead. What it costs is a flattened chain: the
    // next submission of this task chains to a superseded id, which taskStore
    // accepts — two messages off one accepted predecessor are siblings, not a
    // rejection.
    try {
      recordChain(chainFile(context.commonsDir), context.taskId, payload.message.messageId);
    } catch (chainErr) {
      console.error(
        `[${TOOL}] Published, but could not record the chain for task ${context.taskId}: ${chainErr.message}`
      );
    }
    console.log(
      `Accepted: ${first} on task ${context.taskId} — ${stream} entry ${entryId} ` +
      `(message ${payload.message.messageId}, envelope ${envelope.messageId})`
    );
  } catch (err) {
    console.error(`[${TOOL}] Failed to durably enqueue the "${first}" submission:`, err.message);
    process.exitCode = 1;
  } finally {
    await client.quit().catch(() => {});
  }
}

main().catch(err => {
  console.error(`[${TOOL}] ${err.stack || err.message}`);
  process.exit(1);
});
