"""How a record Core returns is rendered to the text its units are cut from
(Corpus Indexing REQ-02), and which of the record's fields each piece of that
text came from (Knowledge Retrieval REQ-04).

Each function takes the record exactly as Core's enumeration returns it
(``services/core/workitems/serializers.py``; the enumeration's body is the
body Core's direct read returns) and returns a ``Rendering``: the text, and
for every field value written into it, where it sits in the text and the
JSON path to that value in the body the passage's ``read`` returns
(``retrieval.sources.address``):

    work item                    GET /work-items/<id>?full=true, from its root
    comment, delivery artifact   the same read, under ``comments`` / ``artifacts``
    A2A message                  GET /a2a/tasks/<task id>, under ``messages``

Only the record's own text is rendered; ids and links stay in the reference,
not the text. Rendering is deterministic, so the same record renders to the
same text and the same fields.

**What a unit's reference says about its fields.** A Core-class unit's
reference carries ``fields``: one entry per field value the passage's text
holds any of, in passage order (``Rendering.fields``)::

    {"path": [<step>, ...], "span": [start, end], "valueOffset": n}

- ``path`` is followed from the root of the read's JSON body. A step is an
  object key (a string), an array index (an integer), or a one-key object
  ``{"<key>": "<value>"}`` meaning "the element of this array whose ``<key>``
  is ``<value>``" (``{"id": ...}`` for a history entry, comment or delivery
  artifact; ``{"messageId": ...}`` for an A2A message). A selected key is
  unique in its array. A message's artifacts are indexed, not selected by
  ``artifactId``: Core keeps the same ``artifactId`` more than once in one
  message (``A2AArtifact``). Its read returns a message's artifacts ordered
  by ``position``, which is unique per message (``uq_a2a_artifact_position``),
  so an index names one artifact; it is the artifact's place in that array,
  not its ``position`` (an artifact Core did not store leaves a gap in
  ``position``). Message parts carry no id and are indexed too.
- ``passage[start:end] == value_text[n:n + (end - start)]``, where
  ``value_text`` is the value at ``path``: a string as it is, any other
  value as ``json.dumps(value, sort_keys=True, ensure_ascii=False)``.
  ``start``, ``end`` and ``n`` count Unicode code points (Python string
  indices), not UTF-16 units or bytes. A value is written whole, so a span
  is shorter than its value only where a unit boundary cuts the value, and
  ``n`` is above 0 only in a unit that starts inside the value.
- **Every character of the passage outside the spans is rendering, not
  record text:** a field's label and ``": "`` (``Description: …``, the
  labels below), the punctuation around an A2A message's role and direction
  or a delivery artifact's type, ``(none)`` where a history entry has no old
  or new value, ``message`` where an A2A message has no role, ``File: ``
  before a file part's name, ``Artifact `` before an artifact's name, and the
  spaces and newlines between values. So a passage holds no record text
  that its ``fields`` do not name.

The unit's ``section``/``offset``/``end`` locate it in this rendering only;
the index uses them to cut and order units. They are not a location in the
read's JSON: ``fields`` is.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

WORK_ITEM_FIELDS = 'fields'
WORK_ITEM_HISTORY = 'history'

_STORY_FIELDS = (('behavior', 'Behavior'), ('acceptance_criteria', 'Acceptance criteria'),
                 ('constraints', 'Constraints'), ('edge_cases', 'Edge cases'), ('out_of_scope', 'Out of scope'),
                 ('value_hypothesis', 'Value hypothesis'), ('test_measurement', 'Test measurement'))
_RELEASE_FIELDS = (('release_notes', 'Release notes'), ('candidate_sha', 'Candidate commit'),
                   ('build_identifier', 'Build'), ('preview_url', 'Preview'))


def _text(value) -> str:
    if value is None:
        return ''
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


@dataclass(frozen=True)
class Rendering:
    """A record's rendered text, and ``spans``: ``(start, end, path)`` for
    each field value written whole at ``text[start:end]``."""

    text: str
    spans: tuple = ()

    def fields(self, offset: int, end: int) -> list[dict]:
        """The ``fields`` of the unit ``text[offset:end]`` (module docstring)."""
        out = []
        for start, stop, path in self.spans:
            lo, hi = max(start, offset), min(stop, end)
            if lo < hi:
                out.append({'path': list(path), 'span': [lo - offset, hi - offset], 'valueOffset': lo - start})
        return out


class _Writer:
    def __init__(self):
        self.parts: list[str] = []
        self.spans: list = []
        self.length = 0

    def label(self, text: str) -> None:
        self.parts.append(text)
        self.length += len(text)

    def value(self, path: tuple, value) -> None:
        text = _text(value)
        if text:
            self.spans.append((self.length, self.length + len(text), tuple(path)))
        self.label(text)

    def extend(self, other: '_Writer') -> None:
        self.spans += [(s + self.length, e + self.length, p) for s, e, p in other.spans]
        self.label(''.join(other.parts))

    def done(self) -> Rendering:
        return Rendering(''.join(self.parts), tuple(self.spans))


def _select(key: str, element, index: int):
    """A path step to ``element`` of an array: by its id where it has one."""
    return {key: element[key]} if isinstance(element, dict) and element.get(key) else index


def _lines(out: _Writer, pairs) -> None:
    for label, path, value in pairs:
        if _text(value).strip():
            out.label(f'{label}: ')
            out.value(path, value)
            out.label('\n')


def work_item(record: dict) -> list[tuple[str, Rendering]]:
    """A work item's fields, then its append-only history (REQ-01, class 2).
    Paths are from the root of the work item's full read."""
    fields = [('Work item', 'display_name'), ('Type', 'type'), ('Status', 'status'), ('Priority', 'priority'),
              ('External key', 'external_key'), ('Assignee', 'assignee_agent_id'), ('Description', 'description')]
    pairs = [(label, (key,), record.get(key)) for label, key in fields]
    story = record.get('storyDetail') or {}
    pairs += [(label, ('storyDetail', key), story.get(key)) for key, label in _STORY_FIELDS]
    release = record.get('releaseDetail') or {}
    pairs += [(label, ('releaseDetail', key), release.get(key)) for key, label in _RELEASE_FIELDS]
    head = _Writer()
    _lines(head, pairs)

    history = _Writer()
    for index, entry in enumerate(record.get('history') or []):
        at = ('history', _select('id', entry, index))
        history.value(at + ('occurred_at',), entry.get('occurred_at'))
        history.label(' ')
        history.value(at + ('actor',), entry.get('actor'))
        history.label(': ')
        history.value(at + ('field',), entry.get('field'))
        for word, key in ((' from ', 'old_value'), (' to ', 'new_value')):
            history.label(word)
            if _text(entry.get(key)):
                history.value(at + (key,), entry.get(key))
            else:
                history.label('(none)')
        history.label('\n')
    sections = [(WORK_ITEM_FIELDS, head.done()), (WORK_ITEM_HISTORY, history.done())]
    return [(name, rendering) for name, rendering in sections if rendering.text.strip()]


def comment(record: dict) -> Rendering:
    """A work-item comment (class 3), under ``comments`` in its work item's
    full read."""
    at = ('comments', _select('id', record, 0))
    out = _Writer()
    _lines(out, [('Comment by', at + ('author',), record.get('author')),
                 ('File', at + ('reference_file',), record.get('reference_file')),
                 ('Function', at + ('reference_function',), record.get('reference_function'))])
    out.value(at + ('body',), record.get('body'))
    return out.done()


def delivery_artifact(record: dict) -> Rendering:
    """A delivery artifact: a commit, PR, CI build or deployment (class 4),
    under ``artifacts`` in its work item's full read."""
    # PostgreSQL's english parser keeps a URL as one lexeme. The words inside it
    # are made searchable by the index's ``search`` column (index.models
    # ``SEARCH``), never by adding them to this text: a passage holds only what
    # the record holds (Corpus Indexing REQ-02, Knowledge Retrieval REQ-04).
    at = ('artifacts', _select('id', record, 0))
    out = _Writer()
    out.label('Delivery artifact (')
    out.value(at + ('artifact_type',), record.get('artifact_type'))
    out.label('): ')
    out.value(at + ('reference',), record.get('reference'))
    out.label('\n')
    return out.done()


def _parts(parts, at: tuple) -> _Writer:
    """A message's or artifact's parts, each on its own line; ``at`` is the
    path to the ``parts`` array."""
    out, first = _Writer(), True
    for index, part in enumerate(parts if isinstance(parts, list) else []):
        if not isinstance(part, dict):
            continue
        kind = part.get('kind') or part.get('type')
        piece = _Writer()
        if kind == 'text' and isinstance(part.get('text'), str):
            piece.value(at + (index, 'text'), part['text'])
        elif kind == 'data':
            piece.value(at + (index, 'data'), part.get('data'))
        elif kind == 'file':
            file = part.get('file') if isinstance(part.get('file'), dict) else {}
            key = 'name' if file.get('name') else 'uri'
            piece.label('File: ')
            piece.value(at + (index, 'file', key), file.get(key))
        if not piece.length:
            continue
        if not first:
            out.label('\n')
        out.extend(piece)
        first = False
    return out


def a2a_message(message: dict) -> Rendering:
    """One A2A message with its artifacts (class 5), under ``messages`` in
    its Task's read. The verdict is not rendered: retrieval reads it from
    Core when a query runs (Knowledge Retrieval REQ-05)."""
    at = ('messages', _select('messageId', message, 0))
    out = _Writer()
    out.label('A2A ')
    if _text(message.get('role')):
        out.value(at + ('role',), message.get('role'))
    else:
        out.label('message')
    out.label(' (')
    out.value(at + ('direction',), message.get('direction'))
    out.label('):\n')
    body = _parts(message.get('parts'), at + ('parts',))
    out.extend(body)
    has_artifact = False
    for index, artifact in enumerate(message.get('artifacts') or []):
        inner = artifact.get('artifact') if isinstance(artifact.get('artifact'), dict) else {}
        artifact_at = at + ('artifacts', index, 'artifact')
        rendered = _parts(inner.get('parts'), artifact_at + ('parts',))
        if rendered.length:
            out.label('\nArtifact ')
            out.value(artifact_at + ('name',), inner.get('name'))
            out.label(':\n')
            out.extend(rendered)
            has_artifact = True
    return out.done() if body.length or has_artifact else Rendering('')
