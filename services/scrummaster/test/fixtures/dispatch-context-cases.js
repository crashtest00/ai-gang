'use strict';

// Fixed work items for Dispatch Context Assembly REQ-02: one per dispatch
// path the four call sites reach. `full` is core's full-record HTTP shape.
// prompt-baseline-bf0dfb5.json holds what the prompt builders produced for
// these at Source bf0dfb5 (track 1's BF-005 merge), before the retrieved
// section existed; dispatchContext.test.js compares the real dispatch of each
// case, with memory-api unreachable, to it byte for byte.

const PROJECT = 'hello-world';

function full(id, over = {}) {
  return {
    id, project: PROJECT, type: 'task', status: 'ready', assignee_agent_id: 'backend-agent',
    external_key: null, parent_id: 'wi-parent-1', display_name: `Work for ${id}`,
    description: 'Add the invoice export endpoint.\nIt must stream CSV.',
    comments: [
      { author: 'Jane', body: 'Use the existing exporter.', created_at: '2026-01-01T00:00:00Z' },
      { author: 'backend-agent', body: 'Understood.', created_at: '2026-01-02T00:00:00Z' },
    ],
    history: [],
    specification_link: { artifact_id: 'spec-billing', requirement_id: 'REQ-03' },
    artifact_links: [{ artifact_id: 'art-1' }, { artifact_id: 'art-2' }],
    ...over,
  };
}

const CASES = [
  { name: 'task', path: 'maybeDispatch', full: full('wi-case-task') },
  {
    name: 'refinement', path: 'maybeDispatch',
    full: full('wi-case-refine', {
      type: 'story', assignee_agent_id: 'refinement-agent',
      storyDetail: { behavior: 'B text', acceptance_criteria: 'AC text', constraints: 'C text', edge_cases: 'E text', out_of_scope: 'O text' },
    }),
  },
  { name: 'unblock', path: 'maybeDispatch', existingTask: true, marker: { file: 'src/x.js', line: 12, text: 'waiting on the schema' }, full: full('wi-case-unblock') },
  {
    name: 'rework', path: 'maybeRedispatchForRework',
    full: full('wi-case-rework', {
      status: 'in-progress',
      history: [{ field: 'status', old_value: 'in-review', new_value: 'in-progress' }],
    }),
  },
  { name: 'blocked-cleared', path: 'handleBlockedClearedSideEffect', marker: null, full: full('wi-case-cleared', { status: 'in-progress' }) },
  {
    name: 'pipeline-failure', path: 'handlePipelineRetry',
    evidence: { kind: 'pipeline_failure', build_url: 'https://jenkins.example.com/job/hello-world/9/', build_number: 9 },
    full: full('wi-case-pipeline', { status: 'in-review' }),
  },
];

module.exports = { CASES, PROJECT };
