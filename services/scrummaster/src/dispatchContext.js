'use strict';

// Dispatch Context Assembly (v6.2): the one place ScrumMaster asks memory
// what it knows about a work item that is about to be dispatched.
//
// Retrieval is consumed through memory-api's route (`POST /retrieve`, Knowledge
// Retrieval REQ-01) exactly as a person would call it. This module selects
// nothing and ranks nothing: it derives the question from the work item
// (REQ-06), sends it with no scope declared (REQ-07), fits the answer into the
// section's bound (REQ-04) and hands back two things:
//
//   retrieved - what prompt.js renders as the reference section, or null
//   record    - what the dispatch message carries in its `metadata`
//               (`memoryRetrieval`), which Core archives whole: the query id
//               and the unit ids actually placed, in order (REQ-08), or why
//               nothing was (REQ-05)
//
// `retrieveForDispatch` never throws. It is awaited at each dispatchTask call
// site BEFORE dispatchTask is called and its result passed in, so the
// read-to-register span inside dispatchTask stays synchronous (the I-07
// window).

const axios = require('axios');
const { buildRetrievedSection } = require('./prompt');

// Provisional values (Open Question 1). The orchestrator measures section size
// and latency on a populated deployment and records the final values in the
// specification.
//
//  - The section bound is the length of the rendered reference section in
//    characters, header, notices and delimiters included.
//  - The timeout bounds the whole call. Memory makes one Core read per distinct
//    source, each bounded at 10 s on its side, so a timeout below that turns a
//    slow Core into an empty section (allowed by REQ-05, recorded as a timeout).
const SECTION_BOUND_CHARS = 6000;
const DEFAULT_TIMEOUT_MS = 10000;
const REQUEST_LIMIT = 20;
const QUERY_MAX_CHARS = 1200;
const FIELD_MAX_CHARS = 400;
// The work done on an answer is bounded independently of its size (REQ-05).
// A body larger than this is refused at the HTTP layer (an `error`); within it,
// only the first REQUEST_LIMIT passages are considered and every string that is
// printed is cut to PRINT_CAP_CHARS. Rendering never shortens a string, so a
// string longer than the section bound can never fit it: the cut changes no
// outcome, it only stops the fit from rendering megabytes to learn that.
const MAX_BODY_BYTES = 4 * 1024 * 1024;
const PRINT_CAP_CHARS = SECTION_BOUND_CHARS + 1;
const REASONS_CAP = 20;

const DEFAULT_MEMORY_API_URL = 'http://memory:9200';

// An address set to the empty string is "no address": retrieval is recorded as
// skipped, not treated as a mode.
function memoryBaseUrl() {
  const raw = process.env.MEMORY_API_URL;
  const value = raw === undefined ? DEFAULT_MEMORY_API_URL : raw.trim();
  return value.replace(/\/+$/, '');
}

function timeoutMs() {
  const parsed = Number.parseInt(process.env.MEMORY_RETRIEVAL_TIMEOUT_MS, 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : DEFAULT_TIMEOUT_MS;
}

function clip(text, max) {
  const value = String(text || '').replace(/\s+/g, ' ').trim();
  return value.length > max ? value.slice(0, max) : value;
}

// The question is derived from the work item being dispatched: its own text
// (title, then its description or, for a story, its behavior and acceptance
// criteria) and its position in the work graph (its parent, and the
// specification requirement and artifacts it is linked to). Two work items
// with different subjects therefore ask different questions. The query is
// capped because a long query costs memory one count per distinct term.
function buildQuery(issue) {
  const parts = [];
  parts.push(clip(issue.summary, FIELD_MAX_CHARS));
  parts.push(clip(issue.description || issue.behavior, FIELD_MAX_CHARS));
  parts.push(clip(issue.acceptanceCriteria, FIELD_MAX_CHARS));
  if (issue.parent) parts.push(`parent ${issue.parent}`);
  if (issue.specificationLink) {
    parts.push(`specification ${issue.specificationLink.artifactId} ${issue.specificationLink.requirementId}`);
  }
  if (issue.artifactLinks && issue.artifactLinks.length > 0) parts.push(issue.artifactLinks.join(' '));
  return parts.filter(Boolean).join('\n').slice(0, QUERY_MAX_CHARS);
}

class RetrievalTimeout extends Error {}

// A hard deadline over the whole call: axios's own `timeout` does not cover a
// connect or DNS stall, so the call is raced against a timer and aborted.
async function postWithDeadline(client, url, body, ms) {
  const controller = new AbortController();
  let timer;
  const deadline = new Promise((_resolve, reject) => {
    timer = setTimeout(() => {
      controller.abort();
      reject(new RetrievalTimeout(`no answer within ${ms} ms`));
    }, ms);
  });
  try {
    const call = Promise.resolve(client.post(url, body, { timeout: ms, signal: controller.signal, maxContentLength: MAX_BODY_BYTES }));
    call.catch(() => {}); // a call that loses the race must not become an unhandled rejection
    return await Promise.race([call, deadline]);
  } finally {
    clearTimeout(timer);
  }
}

function cap(value) {
  return typeof value === 'string' && value.length > PRINT_CAP_CHARS ? value.slice(0, PRINT_CAP_CHARS) : value;
}

// A passage whose printed strings are cut to PRINT_CAP_CHARS; a well-formed one
// is returned unchanged in content.
function boundPassage(p) {
  if (!p || typeof p !== 'object') return p; // malformed: the render guard reports it
  const out = { ...p };
  for (const key of ['text', 'scope', 'class', 'writtenAt', 'rank']) out[key] = cap(p[key]);
  if (p.source && typeof p.source === 'object') {
    out.source = { ...p.source };
    for (const key of ['owner', 'read', 'record']) out.source[key] = cap(p.source[key]);
  }
  if (p.sourceStatus !== undefined && p.sourceStatus !== null) {
    const json = JSON.stringify(p.sourceStatus);
    if (json && json.length > PRINT_CAP_CHARS) out.sourceStatus = json.slice(0, PRINT_CAP_CHARS);
  }
  return out;
}

function shape(response) {
  const reasons = response.incompleteReasons || [];
  return {
    notice: cap(response.notice),
    incomplete: !!response.incomplete,
    incompleteReasons: Array.isArray(reasons) ? reasons.slice(0, REASONS_CAP).map(cap) : reasons,
    withheldCount: Array.isArray(response.withheld) ? response.withheld.length : 0,
  };
}

// Highest-ranked passages first; a passage is placed when the whole rendered
// section still fits the bound with it, and left out (counted, and stated in
// the prompt) when it does not. The work item's own fields are rendered by the
// builders around this section and are never part of this fit.
function fit(passages, base, returnedCount = passages.length) {
  const placed = [];
  for (const passage of passages) {
    const trial = [...placed, passage];
    const candidate = { ...base, passages: trial, returnedCount,
      omittedCount: returnedCount - trial.length, boundChars: SECTION_BOUND_CHARS };
    if (buildRetrievedSection(candidate).join('\n').length <= SECTION_BOUND_CHARS) placed.push(passage);
  }
  return { ...base, passages: placed, returnedCount,
    omittedCount: returnedCount - placed.length, boundChars: SECTION_BOUND_CHARS };
}

function failure(status, reason, startedAt, extra = {}) {
  console.error(`[retrieval] No retrieved context for this dispatch (${status}): ${reason}`);
  return { retrieved: null, record: { status, reason, ...extra, elapsedMs: Date.now() - startedAt } };
}

async function retrieveForDispatch(issue, { client = axios } = {}) {
  const startedAt = Date.now();
  const base = memoryBaseUrl();
  if (!base) return failure('skipped', 'MEMORY_API_URL is empty, so retrieval has no address', startedAt);

  const question = buildQuery(issue);
  if (!question) return failure('skipped', 'the work item has no text to derive a question from', startedAt);

  let response;
  try {
    // No `scope`: dispatch covers both `project` and `platform` (REQ-07).
    const result = await postWithDeadline(client, `${base}/retrieve`, { question, limit: REQUEST_LIMIT }, timeoutMs());
    response = result && result.data;
  } catch (err) {
    if (err instanceof RetrievalTimeout) return failure('timeout', err.message, startedAt);
    const detail = err.response ? `HTTP ${err.response.status}` : (err.code || err.message);
    return failure('error', `memory-api could not be read: ${detail}`, startedAt);
  }
  if (!response || !Array.isArray(response.passages)) {
    return failure('error', 'memory-api answered with a body that has no passages list', startedAt);
  }

  // Shaping, fitting and rendering the answer run inside this guard: a body
  // that has a passages list but malformed contents (a null passage, a
  // non-list incompleteReasons) is a retrieval error like any other, so the
  // dispatch proceeds with its own fields alone (REQ-05).
  const queryId = response.queryId === undefined ? null : response.queryId;
  try {
    const answer = shape(response);
    const common = {
      queryId,
      incomplete: answer.incomplete,
      incompleteReasons: answer.incompleteReasons,
      withheldCount: answer.withheldCount,
      returnedCount: response.passages.length,
      elapsedMs: Date.now() - startedAt,
    };
    if (response.passages.length === 0) {
      console.log(`[retrieval] Memory returned nothing for this dispatch (query ${common.queryId})`);
      return { retrieved: null, record: { status: 'empty', ...common, placedUnitIds: [], omittedCount: 0 } };
    }

    const retrieved = fit(response.passages.slice(0, REQUEST_LIMIT).map(boundPassage), answer, response.passages.length);
    buildRetrievedSection(retrieved); // the section handlers.js will render must render here first
    return {
      retrieved,
      record: {
        status: 'placed',
        ...common,
        placedUnitIds: retrieved.passages.map(p => p.unitId),
        omittedCount: retrieved.omittedCount,
        boundChars: SECTION_BOUND_CHARS,
      },
    };
  } catch (err) {
    return failure('error', `memory-api answered with a malformed body: ${err.message}`, startedAt, { queryId });
  }
}

module.exports = { retrieveForDispatch };
