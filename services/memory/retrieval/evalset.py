"""The retrieval evaluation set (Knowledge Retrieval REQ-09).

This module exists before any ranking code and is not changed to make a
ranker pass. It holds:

- ``CORPUS``: a fixed set of passages, at least one per source class, with
  the ``session`` class deliberately over-represented (REQ-10) and a
  ``platform`` pair (REQ-06);
- ``QUESTIONS``: natural-language questions, each paired with the passages
  that correctly answer it. A question whose ``relevant`` list is empty is
  one whose right answer is "nothing relevant exists", so a ranker that
  returns something plausible-looking for everything fails (REQ-09);
- ``METRICS`` and ``THRESHOLDS``: what is measured and what constitutes
  passing;
- ``seed()`` and ``evaluate()``: writing the corpus into the index through
  ``index.store`` (as the indexer does) and scoring a ranker against it.

Metric and threshold (Knowledge Retrieval Open Question 1):

    hit_at_3      over the answerable questions, the share whose first three
                  passages include a correct one            >= 0.90
    mrr           over the answerable questions, the mean of 1 / (rank of
                  the first correct passage), 0 if absent     >= 0.80
    abstention    over the unanswerable questions, the share for which the
                  ranker returns no passage                   >= 0.75
    class_hit     the lowest hit_at_3 of any one source class >= 0.66

The questions never name their source or its id (Corpus Indexing REQ-01).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

PROJECT = 'project'
PLATFORM = 'platform'

# class names are the index's (index.models); repeated here as plain strings
# so this module does not depend on the code that ranks.
SPEC, WORK_ITEM, COMMENT, DELIVERY, A2A, SESSION, LIBRARY, PLATFORM_DOC = (
    'specification', 'work_item', 'work_item_comment', 'delivery_artifact', 'a2a_message', 'session',
    'library_file', 'platform_document')

SEVEN_CLASSES = (SPEC, WORK_ITEM, COMMENT, DELIVERY, A2A, SESSION, LIBRARY)


@dataclass(frozen=True)
class Passage:
    id: str
    source_class: str
    text: str

    @property
    def scope(self) -> str:
        return PLATFORM if self.source_class == PLATFORM_DOC else PROJECT


CORPUS: list[Passage] = [
    # --- specifications (class 1) ---
    Passage('s1', SPEC, 'REQ-04: The gateway MUST retry a failed delivery three times with exponential '
                        'backoff, starting at two seconds, before the message is dead-lettered.'),
    Passage('s2', SPEC, 'REQ-07: Session tokens expire after twelve hours of inactivity and are rotated '
                        'on every privilege change.'),
    Passage('s3', SPEC, 'REQ-02: The invoice export MUST be produced as CSV with a UTF-8 byte order mark '
                        'so spreadsheet tools open it correctly.'),
    Passage('s4', SPEC, 'REQ-11: A refund above five hundred dollars requires approval from a second '
                        'administrator before it is issued.'),
    Passage('s5', SPEC, 'REQ-09: The audit trail records the acting user, the record changed and the '
                        'previous value, and cannot be edited through the application.'),
    # --- work items and their history (class 2) ---
    Passage('w1', WORK_ITEM, 'Fix duplicate shipment notifications. Customers receive two emails when a '
                             'shipment is marked delivered because the webhook handler is registered twice.'),
    Passage('w2', WORK_ITEM, 'Add pagination to the supplier list. The supplier endpoint returns every row '
                             'and times out for tenants with more than ten thousand suppliers.'),
    Passage('w3', WORK_ITEM, 'Investigate nightly backup failure on the reporting database; the dump '
                             'exceeds the disk quota of the backup volume.'),
    Passage('w4', WORK_ITEM, 'Replace the legacy PDF renderer with the headless browser renderer to fix '
                             'broken table borders in printed quotes.'),
    Passage('w5', WORK_ITEM, 'Rate limit the public search endpoint to sixty requests per minute per API key.'),
    Passage('w6', WORK_ITEM, 'History: status from in_progress to blocked by backend-agent: waiting for the '
                             'certificate renewal from the infrastructure team.'),
    # --- work-item comments (class 3) ---
    Passage('c1', COMMENT, 'The root cause was a stale connection pool: after the database failover the '
                           'pool kept handing out sockets to the old primary.'),
    Passage('c2', COMMENT, 'I reproduced the rounding error: prices stored as floats drift by a cent after '
                           'about forty additions. Switching to integer cents fixes it.'),
    Passage('c3', COMMENT, 'Reviewer note: the migration locks the orders table for several minutes on '
                           'production size data, please add the index concurrently.'),
    Passage('c4', COMMENT, 'We decided against caching the permission lookups because stale grants would '
                           'let a removed user keep access for the cache lifetime.'),
    # --- delivery artifacts (class 4) ---
    Passage('d1', DELIVERY, 'commit 4f2a9c1 fix: retry the payment capture when the processor returns a '
                            'gateway timeout'),
    Passage('d2', DELIVERY, 'pull request 218 merged: introduce read replica routing for the reporting queries'),
    Passage('d3', DELIVERY, 'CI build 1042 failed: test_invoice_totals expected 1999 but got 2000 in the '
                            'currency rounding suite'),
    Passage('d4', DELIVERY, 'deployment to staging succeeded: release 2.8.1 with the new onboarding checklist'),
    # --- A2A messages (class 5) ---
    Passage('a1', A2A, 'A2A agent message: please review the proposal to move the scheduler to a '
                       'leader-elected worker, so only one instance enqueues recurring jobs.'),
    Passage('a2', A2A, 'A2A agent message: the integration tests are flaky because the sandbox mail server '
                       'rejects more than ten messages a minute.'),
    Passage('a3', A2A, 'A2A agent message: I changed the order status enum; the fulfilment service must '
                       'handle the new value awaiting_pickup.'),
    # --- session turns and notes (class 6), many of them ---
    Passage('x1', SESSION, 'Tried increasing the worker memory limit to 2 GB; the out-of-memory kills '
                           'stopped, so the leak is in the image resizing library.'),
    Passage('x2', SESSION, 'The tests failed because the timezone of the CI machine is UTC while the '
                           'fixture assumed Chicago; set TZ in the test runner.'),
    Passage('x3', SESSION, 'npm ci failed with ERESOLVE due to a peer dependency conflict between the '
                           'charting library and react 18; pinned the charting library to 4.2.'),
    Passage('x4', SESSION, 'Note: always run the linter before pushing, the pre-push hook is slow on the '
                           'monorepo.'),
    Passage('x5', SESSION, 'Looking at the gateway retry behaviour again: a failed delivery is retried with '
                           'exponential backoff and then dead-lettered.'),
    Passage('x6', SESSION, 'The gateway retried the failed delivery three times in the log, then the '
                           'message went to the dead-letter queue.'),
    Passage('x7', SESSION, 'Checked how many times the gateway retries a delivery; the backoff is '
                           'exponential, so the third retry waits eight seconds.'),
    Passage('x8', SESSION, 'Reading the gateway code: retry count is read from config, backoff doubles on '
                           'every failed delivery attempt.'),
    Passage('x9', SESSION, 'Added a test for gateway delivery retries: it fails the first three attempts '
                           'and expects the dead-letter after the backoff.'),
    Passage('x10', SESSION, 'The gateway delivery retry log shows exponential backoff of two, four and '
                            'eight seconds before giving up.'),
    Passage('x11', SESSION, 'Debugging why the gateway gives up on delivery: retries are exhausted after '
                            'three failed attempts with backoff.'),
    Passage('x12', SESSION, 'Discussed the retry policy of the gateway with the user; they confirmed '
                            'failed deliveries retry then dead-letter.'),
    Passage('x13', SESSION, 'User asked how long session tokens last; I looked: they expire after twelve '
                            'hours of inactivity.'),
    Passage('x14', SESSION, 'Session token expiry: inactivity of twelve hours expires the token, and '
                            'privilege changes rotate tokens.'),
    Passage('x15', SESSION, 'Wrote a test that session tokens expire after twelve hours without activity '
                            'and are rotated on privilege change.'),
    Passage('x16', SESSION, 'Fixed the session token rotation bug: a privilege change did not rotate the '
                            'token after twelve hours of inactivity.'),
    # --- artifact-library files (class 7) ---
    Passage('l1', LIBRARY, 'Supplier onboarding handbook: new suppliers must submit a tax certificate and a '
                           'bank letter before the first purchase order can be approved.'),
    Passage('l2', LIBRARY, 'Quarterly security review: penetration testers found that the password reset '
                           'link could be reused within its validity window.'),
    Passage('l3', LIBRARY, 'Office relocation plan: the data centre cage moves on the third weekend of '
                           'March, with a four hour maintenance window.'),
    # --- AI Gang's own specification corpus (scope platform) ---
    Passage('p1', PLATFORM_DOC, 'To reset the agent workspace run the reset script; it removes the worktree, '
                                'deletes the branch and recreates the container volume.'),
    Passage('p2', PLATFORM_DOC, 'Every project instance runs on its own virtual machine; instances never '
                                'share a database or a Redis stream.'),
    Passage('p3', PLATFORM_DOC, 'The dispatch subscriber enforces a thirty minute limit on a single agent '
                                'run and marks longer runs as timed out.'),
]


@dataclass(frozen=True)
class Question:
    text: str
    relevant: tuple = ()
    # What the question is for, so a failure reads as a finding.
    note: str = ''


def _q(text, *relevant, note=''):
    return Question(text, tuple(relevant), note)


QUESTIONS: list[Question] = [
    # specifications; the first two have many session passages saying the same (REQ-10)
    _q('How many times does the gateway retry a failed delivery before giving up?', 's1', note='crowded by sessions'),
    _q('When do user sessions time out?', 's2', note='crowded by sessions'),
    _q('What file format and encoding must the invoice export use?', 's3'),
    _q('Who must approve a big refund?', 's4'),
    _q('Can people edit the audit log from the application?', 's5'),
    # work items
    _q('Why were customers getting two shipment emails?', 'w1'),
    _q('Why does the supplier page time out for big tenants?', 'w2'),
    _q('What was wrong with the nightly reporting backup?', 'w3'),
    _q('Why are the borders in printed quotes broken?', 'w4'),
    _q('What is the request limit on public search?', 'w5'),
    _q('Which work is blocked waiting on a certificate?', 'w6'),
    # comments
    _q('What caused the stale connections after the database failover?', 'c1'),
    _q('Why do prices drift by a cent?', 'c2'),
    _q('What was the problem with the orders table migration on production?', 'c3'),
    _q('Why was caching the permission lookups rejected?', 'c4'),
    # delivery artifacts
    _q('Which commit retries the payment capture on a gateway timeout?', 'd1'),
    _q('Which change added read replica routing?', 'd2'),
    _q('Which test expected 1999 and got 2000 for invoice totals?', 'd3'),
    _q('Which release added the onboarding checklist?', 'd4'),
    # A2A messages
    _q('Who proposed making the scheduler leader-elected?', 'a1'),
    _q('Why are the integration tests flaky with the mail server?', 'a2'),
    _q('What new order status must the fulfilment service handle?', 'a3'),
    # sessions
    _q('What stopped the out-of-memory kills of the workers?', 'x1'),
    _q('Why did the tests fail on the CI machine because of dates?', 'x2'),
    _q('How was the charting dependency conflict resolved?', 'x3'),
    _q('What should I run before pushing?', 'x4'),
    # library files
    _q('What must a new supplier provide before the first purchase order?', 'l1'),
    _q('What vulnerability affected the password reset links?', 'l2'),
    _q('When does the data centre cage move?', 'l3'),
    # platform
    _q('How do I reset the agent workspace?', 'p1'),
    _q('Do projects share a database?', 'p2'),
    _q('How long can an agent run last?', 'p3'),
    # nothing relevant exists (REQ-09): some share a generic term with the corpus
    _q('What colour is the CEO company car?', note='no shared term'),
    _q('Which cloud region hosts the analytics warehouse?', note='no shared term'),
    _q('How many vacation days do employees get?', note='no shared term'),
    _q('What is the password for the production database?', note='shares "password" and "database"'),
    _q('When is the next team offsite?', note='no shared term'),
    _q('Which programming language is the mobile app written in?', note='no shared term'),
    _q('What is the refund policy for digital goods?', note='shares "refund"'),
    _q('Who won the football match last night?', note='no shared term'),
]

THRESHOLDS = {'hit_at_3': 0.90, 'mrr': 0.80, 'abstention': 0.75, 'class_hit': 0.66}
TOP_K = 3

PASSAGES = {p.id: p for p in CORPUS}


def _reference(passage: Passage, text: str) -> dict:
    ns = uuid.UUID('6c2c3f4e-0f2e-4b9a-9d41-2d6d6c3a7e10')
    ident = str(uuid.uuid5(ns, passage.id))
    span = {'offset': 0, 'end': len(text)}
    lines = {'lineStart': 1, 'lineEnd': 1}
    cls = passage.source_class
    if cls in (LIBRARY, SPEC):
        return {'artifactId': ident, 'stamp': {'updatedAt': '2026-10-01T00:00:00+00:00', 'sha256': 'e' * 64},
                **lines, **span}
    if cls == SESSION:
        return {'sessionId': ident, 'file': f'sessions/{ident}.jsonl', 'turn': 1, 'lines': [1], **lines, **span}
    if cls == WORK_ITEM:
        return {'workItemId': ident, 'section': 'fields', **span}
    if cls == COMMENT:
        return {'commentId': ident, 'workItemId': ident, **span}
    if cls == DELIVERY:
        return {'deliveryArtifactId': ident, 'workItemId': ident, **span}
    if cls == A2A:
        return {'taskId': ident, 'messageId': ident, **span}
    return {'commit': 'c' * 40, 'path': f'docs/{passage.id}.md', **lines, **span}


def source_key(passage_id: str) -> str:
    return f'eval:{passage_id}'


def seed(corpus=None) -> None:
    """Write the corpus into the index as the indexer does (``index.store``)."""
    from index import store
    for passage in corpus or CORPUS:
        store.replace_units(source_key(passage.id), passage.source_class, {'eval': passage.id},
                            [{'reference': _reference(passage, passage.text), 'text': passage.text}])


# --- The metric ---------------------------------------------------------------

@dataclass
class Result:
    scores: dict
    per_class: dict
    failures: list = field(default_factory=list)

    def passes(self, thresholds=None) -> bool:
        return not self.misses(thresholds)

    def misses(self, thresholds=None) -> list:
        wanted = thresholds or THRESHOLDS
        key = {'hit_at_3': 'hit_at_3', 'mrr': 'mrr', 'abstention': 'abstention', 'class_hit': 'class_hit'}
        return [f'{name} {self.scores[key[name]]:.3f} < {value}' for name, value in wanted.items()
                if self.scores[key[name]] < value]


def evaluate(rank: Callable[[str], list]) -> Result:
    """Score a ranker. ``rank(question)`` returns the ids of the passages it
    returns, best first, as passage ids of ``CORPUS``."""
    answerable = [q for q in QUESTIONS if q.relevant]
    unanswerable = [q for q in QUESTIONS if not q.relevant]
    hits, rr, failures = 0, 0.0, []
    per_class: dict = {}
    for q in answerable:
        ranked = rank(q.text)
        position = next((i + 1 for i, pid in enumerate(ranked) if pid in q.relevant), None)
        hit = position is not None and position <= TOP_K
        hits += hit
        rr += 1.0 / position if position else 0.0
        cls = PASSAGES[q.relevant[0]].source_class
        counts = per_class.setdefault(cls, [0, 0])
        counts[0] += hit
        counts[1] += 1
        if not hit:
            failures.append(f'{q.text!r}: wanted {q.relevant}, got {ranked[:TOP_K]}')
    abstained = 0
    for q in unanswerable:
        ranked = rank(q.text)
        if not ranked:
            abstained += 1
        else:
            failures.append(f'{q.text!r}: nothing relevant exists, got {ranked[:TOP_K]}')
    class_hit = {cls: h / n for cls, (h, n) in per_class.items()}
    return Result(
        scores={'hit_at_3': hits / len(answerable), 'mrr': rr / len(answerable),
                'abstention': abstained / len(unanswerable), 'class_hit': min(class_hit.values())},
        per_class=class_hit, failures=failures)
