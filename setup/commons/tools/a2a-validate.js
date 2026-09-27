'use strict';

/**
 * Construction-time validation for everything published onto
 * `aigang:gateway:{project}` (V5.0 Deterministic Gateway Message Tooling
 * REQ-01, REQ-02, REQ-07).
 *
 * Both entry points of the publish tool run this before any `XADD`, so no
 * producer has to remember to call it first:
 *
 *   a2a-submit.js       the constructor an agent invokes with an operation
 *                       and its fields; validates what it built
 *   gateway-publish.js  the raw file/stdin entry point Jenkins and any
 *                       producer that already holds a message uses
 *
 * What it checks mirrors what the server does with the same payload, and it
 * is routed the same way: `services/scrummaster/src/gateway.js`'s
 * `dispatchGatewayOperation` branches on `operation === 'materializeDecomposition'`,
 * then `type === 'pipeline_retry'`, then falls through to
 * `handleA2ASubmission`. `DEFINITIONS` below is that routing table, and
 * adding validation for a further structured action is a new entry in it —
 * no second checker and no third entry point (REQ-07).
 *
 * Two rules here are deliberately stricter than the gateway handler, both
 * product-owner-approved (READINESS_DECISIONS.md item 4, spec REQ-02): a
 * `create_subtask` request must carry `description`, and it must carry
 * `agentFieldValue`. Source states both as required in every place an agent
 * can read them (setup/SCRUMMASTER_SPEC_v1.md's operations table,
 * setup/agents/refinement-agent.md); the handler's defaulting and its
 * derive-from-the-summary recovery stay in place as the platform's backstop
 * for producers that do not use this tool.
 *
 * Two further rules refuse here what the gateway refuses *after* the durable
 * write, which is the silent server-side refusal REQ-01 exists to close (V5.0
 * audit row 37): a submission's `role` must be `agent`, not merely one of A2A's
 * two roles, and a `data.operation` must be one the gateway routes. In both
 * cases the server drops the message with a `console.warn` no producer reads,
 * having already written it to the stream. Nothing else here refuses input the
 * gateway acts on.
 */

const schema = require('./a2a-schema');

class GatewayValidationError extends Error {
  constructor(kind, errors) {
    super(`Invalid ${kind}: ${errors.join('; ')}`);
    this.name = 'GatewayValidationError';
    this.kind = kind;
    this.errors = errors;
  }
}

function isPlainObject(v) {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

function isNonEmptyString(v) {
  return typeof v === 'string' && v.length > 0;
}

// ---------------------------------------------------------------------------
// The A2A submission: `{ state, message, artifacts? }`
// ---------------------------------------------------------------------------

// The `data` Part carries the requested operation and its fields; the gateway
// reads it the same way (`gateway.js` `handleA2ASubmission`).
function dataOf(message) {
  const part = (message && Array.isArray(message.parts) ? message.parts : [])
    .find(p => isPlainObject(p) && p.kind === 'data');
  return isPlainObject(part) && isPlainObject(part.data) ? part.data : null;
}

// `data.reference` is optional everywhere and consumed on three paths (a
// `comment` operation, a working-state submission with no `operation`, and the
// `input-required`/`auth-required` states). Producer documents show the object
// form; `gateway.js`'s formatReference also accepts a bare string, so both are
// well-formed here — shape only, and only when present.
function checkReference(reference, errors, path) {
  if (reference === undefined || reference === null) return;
  if (typeof reference === 'string') {
    if (!isNonEmptyString(reference)) errors.push(`${path} must be a non-empty string when given as a string`);
    return;
  }
  if (!isPlainObject(reference)) {
    errors.push(`${path} must be an object with a non-empty "file" string (an optional "function" string), or a non-empty string`);
    return;
  }
  if (!isNonEmptyString(reference.file)) errors.push(`${path}.file must be a non-empty string`);
  if (reference.function !== undefined && reference.function !== null && !isNonEmptyString(reference.function)) {
    errors.push(`${path}.function must be a non-empty string when present`);
  }
}

// v4.1 agent-artifact-automation.md REQ-01, mirrored from `gateway.js`'s
// validateReferenceShape: shape only, never whether an artifact id resolves —
// that depends on server-side state this tool cannot see.
function checkSubtaskReferences(data, errors, path) {
  const { specificationLink, artifactLinks } = data;

  if (specificationLink !== undefined && specificationLink !== null) {
    const wellFormed = isPlainObject(specificationLink)
      && isNonEmptyString(specificationLink.artifactId)
      && isNonEmptyString(specificationLink.requirementId);
    if (!wellFormed) {
      errors.push(`${path}.specificationLink is present but is not an object with non-empty "artifactId" and "requirementId" strings`);
    }
  }

  if (artifactLinks !== undefined && artifactLinks !== null) {
    const wellFormed = Array.isArray(artifactLinks) && artifactLinks.every(isNonEmptyString);
    if (!wellFormed) {
      errors.push(`${path}.artifactLinks is present but is not an array of non-empty artifact id strings`);
    }
  }
}

// The operations `gateway.js`'s `handleA2ASubmission` switch routes. Anything
// else falls to its `default`, which calls reportUnsupportedOperation — after
// the entry is already durably on the stream. `undefined` is the plain-progress
// path and is not in this list because absence is not a value.
const ROUTED_OPERATIONS = ['comment', 'reassign', 'create_subtask'];

// `handleA2ASubmission` only reaches that switch, and so only reads
// `data.operation`, in the non-terminal, non-interrupted branch: a
// `completed`/`failed`/`canceled`/`rejected` submission is handled by
// handleCompleted/handleTerminalFailure, and an `input-required`/
// `auth-required` one by handleInterrupted — neither branch looks at
// `operation` at all, routed or not. Refusing an unrouted operation on one
// of those submissions would refuse input the gateway simply ignores,
// which the header above promises this file does not do.
function gatewayReadsOperation(state) {
  return !schema.TERMINAL_STATES.includes(state) && !schema.INTERRUPTED_STATES.includes(state);
}

// REQ-02 — the fields the corresponding gateway handler requires for the
// requested operation, on top of the message shape REQ-01 checks.
function checkOperationFields(payload, errors) {
  const data = dataOf(payload.message);
  const path = 'message.parts[data].data';

  if (data) checkReference(data.reference, errors, `${path}.reference`);

  const operation = data ? data.operation : undefined;

  if (gatewayReadsOperation(payload.state) && operation !== undefined && !ROUTED_OPERATIONS.includes(operation)) {
    errors.push(
      `${path}.operation must be one of ${ROUTED_OPERATIONS.join('/')}, or absent for a plain progress note` +
      ` — the gateway refuses any other operation after the submission is already durably written`
    );
  }

  if (operation === 'create_subtask') {
    if (!isNonEmptyString(data.summary)) errors.push(`${path}.summary must be a non-empty string for the "create_subtask" operation`);
    // Stricter than the handler, by approved decision — see the header.
    if (!isNonEmptyString(data.description)) errors.push(`${path}.description must be a non-empty string for the "create_subtask" operation`);
    if (!isNonEmptyString(data.agentFieldValue)) errors.push(`${path}.agentFieldValue must be a non-empty string for the "create_subtask" operation`);
    checkSubtaskReferences(data, errors, path);
  }

  if (operation === 'reassign') {
    if (!isNonEmptyString(data.agentFieldValue)) errors.push(`${path}.agentFieldValue must be a non-empty string for the "reassign" operation`);
  }

  // A `completed` submission's optional pull-request Artifact: `handleCompleted`
  // reads the PR url out of its `file` Part, so an artifact by that name with
  // no reachable uri posts a comment that says "PR opened" and names nothing.
  if (payload.state === 'completed') {
    for (const [i, artifact] of (payload.artifacts || []).entries()) {
      if (!isPlainObject(artifact) || artifact.name !== 'pull-request') continue;
      const filePart = (Array.isArray(artifact.parts) ? artifact.parts : [])
        .find(p => isPlainObject(p) && p.kind === 'file');
      if (!filePart || !isNonEmptyString(filePart.file && filePart.file.uri)) {
        errors.push(`artifacts[${i}] is named "pull-request" and must carry a file Part whose file.uri is a non-empty string`);
      }
    }
  }
}

// REQ-01 — the submission's own `state`, the message shape in full, and every
// artifact, by the same rules a2a-schema.js enforces server-side.
function checkA2ASubmission(payload, errors) {
  if (!schema.TASK_STATES.includes(payload.state)) {
    errors.push(`state must be one of ${schema.TASK_STATES.join('/')}`);
  }
  try {
    schema.validateMessage(payload.message);
  } catch (err) {
    errors.push(err.message);
  }
  // Stricter than `schema.MESSAGE_ROLES`, which allows both A2A roles because
  // the schema describes messages in both directions. A submission is an agent
  // reporting on its own task: `handleA2ASubmission` drops any other role with a
  // `console.warn`, after the durable write, so `role: 'client'` is accepted
  // here today and silently does nothing.
  if (isPlainObject(payload.message) && payload.message.role !== 'agent') {
    errors.push('message.role must be "agent" — a submission is an agent reporting on its own task, and the gateway drops any other role after the submission is already durably written');
  }
  for (const [i, artifact] of (payload.artifacts || []).entries()) {
    try {
      schema.validateArtifact(artifact, `artifacts[${i}]`);
    } catch (err) {
      errors.push(err.message);
    }
  }
  // Only worth reading the data Part once the message itself is sound.
  if (errors.length === 0) checkOperationFields(payload, errors);
}

// ---------------------------------------------------------------------------
// The routing table (REQ-07)
// ---------------------------------------------------------------------------

// One entry per structured action carried on the gateway stream, in the order
// `gateway.js`'s dispatchGatewayOperation tests them. `matches` is that
// function's own branch condition; `check` pushes a field-naming message per
// problem. Extending the tool to a further action is an entry here and nothing
// else. The last entry has no `matches`: it is the fall-through, the same way
// `handleA2ASubmission` is the server's.
//
// "An entry here and nothing else" is true of the *validation*, with two
// footnotes about the rest of the tool (V5.0 audit row 48):
//
//  1. The transport envelope's `taskId` comes from `gateway-publish.js`'s own
//     fallback chain (`payload.message?.taskId || payload.ticket_key || ...`),
//     so a further action that carries its own id field needs that chain
//     extended too, or its entries reach the stream with a null taskId.
//  2. Both entry points hard-code `kind: KIND.JIRA_OPERATION`
//     (`a2a-submit.js`, `gateway-publish.js`), which makes
//     dispatchGatewayOperation's `KIND.TASK_STATUS` branch unreachable from
//     either of them — a future `task_status` producer needs a `kind` argument,
//     not just an entry below.
const DEFINITIONS = [
  {
    // `gateway.js` -> handleMaterializeDecomposition -> dependencies.js
    // materializeDecomposition, which requires exactly these two. The array
    // may be empty, as it may be there: anything narrower would refuse a
    // payload the gateway accepts.
    id: 'materializeDecomposition',
    matches: payload => payload.operation === 'materializeDecomposition',
    check(payload, errors) {
      if (!isNonEmptyString(payload.parentJiraIssueKey)) {
        errors.push('parentJiraIssueKey must be a non-empty string for the "materializeDecomposition" operation');
      }
      if (!Array.isArray(payload.subtasks)) {
        errors.push('subtasks must be an array for the "materializeDecomposition" operation');
      }
    },
  },
  {
    // Jenkins' pipeline-failure handler (setup/Jenkinsfile.template's
    // `post { failure { ... } }`) -> gateway.js handlePipelineRetry, which
    // reads ticket_key, build_url and build_number.
    id: 'pipeline_retry',
    matches: payload => payload.type === 'pipeline_retry',
    check(payload, errors) {
      if (!isNonEmptyString(payload.ticket_key)) {
        errors.push('ticket_key must be a non-empty string for a "pipeline_retry" message');
      }
      // handlePipelineRetry defaults both to null, so they are optional; an
      // empty string is what a broken `jq` interpolation produces, and that is
      // worth naming rather than forwarding.
      for (const field of ['build_url', 'build_number']) {
        const value = payload[field];
        if (value !== undefined && value !== null && !isNonEmptyString(value) && typeof value !== 'number') {
          errors.push(`${field} must be a non-empty string when present on a "pipeline_retry" message`);
        }
      }
    },
  },
  {
    id: 'a2a_submission',
    check: checkA2ASubmission,
  },
];

function definitionFor(payload) {
  return DEFINITIONS.find(d => !d.matches || d.matches(payload));
}

/**
 * Validate one gateway payload, whichever producer built it. Returns the
 * payload unchanged; throws GatewayValidationError, whose `.errors` is one
 * field-naming message per problem, when it is not publishable.
 */
function validateGatewayPayload(payload) {
  if (!isPlainObject(payload)) {
    throw new GatewayValidationError('gateway payload', ['the payload must be a JSON object']);
  }
  const definition = definitionFor(payload);
  const errors = [];
  definition.check(payload, errors);
  if (errors.length) throw new GatewayValidationError(definition.id, errors);
  return payload;
}

module.exports = {
  GatewayValidationError,
  validateGatewayPayload,
};
