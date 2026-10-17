"""How a record Core returns is rendered to the text its units are cut from
(Corpus Indexing REQ-02).

Rendering is deterministic: the same record renders to the same text, so a
unit's reference (the record's id, the section, and ``offset``/``end`` in
that section's rendering) resolves by reading the record again through
Core's read interface and slicing its rendering. Each function takes the
record exactly as Core's enumeration returns it (``services/core/workitems/
serializers.py``) and returns ``[(section, text)]``. Only the record's own
text is rendered. Ids and links stay in the reference, not the text.
"""

from __future__ import annotations

import json
import re

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


def _lines(pairs) -> str:
    return ''.join(f'{label}: {_text(value)}\n' for label, value in pairs if _text(value).strip())


def work_item(record: dict) -> list[tuple[str, str]]:
    """A work item's fields, then its append-only history (REQ-01, class 2)."""
    fields = [('Work item', record.get('display_name')), ('Type', record.get('type')),
              ('Status', record.get('status')), ('Priority', record.get('priority')),
              ('External key', record.get('external_key')), ('Assignee', record.get('assignee_agent_id')),
              ('Description', record.get('description'))]
    story = record.get('storyDetail') or {}
    fields += [(label, story.get(key)) for key, label in _STORY_FIELDS]
    release = record.get('releaseDetail') or {}
    fields += [(label, release.get(key)) for key, label in _RELEASE_FIELDS]
    sections = [(WORK_ITEM_FIELDS, _lines(fields))]
    history = ''.join(
        f"{h.get('occurred_at') or ''} {h.get('actor') or ''}: {h.get('field')} "
        f"from {_text(h.get('old_value')) or '(none)'} to {_text(h.get('new_value')) or '(none)'}\n"
        for h in record.get('history') or [])
    sections.append((WORK_ITEM_HISTORY, history))
    return [(name, text) for name, text in sections if text.strip()]


def comment(record: dict) -> str:
    """A work-item comment (class 3)."""
    head = _lines([('Comment by', record.get('author')), ('File', record.get('reference_file')),
                   ('Function', record.get('reference_function'))])
    return head + _text(record.get('body'))


def delivery_artifact(record: dict) -> str:
    """A delivery artifact: a commit, PR, CI build or deployment (class 4)."""
    reference = _text(record.get('reference'))
    # PostgreSQL's english parser keeps a URL as one lexeme, so the words inside it
    # are also written out on their own line: a plain question can then match them.
    words = ' '.join(re.findall(r'[A-Za-z0-9]+', reference))
    return f"Delivery artifact ({_text(record.get('artifact_type'))}): {reference}\n{words}\n"


def _parts(parts) -> str:
    out = []
    for part in parts if isinstance(parts, list) else []:
        if not isinstance(part, dict):
            continue
        kind = part.get('kind') or part.get('type')
        if kind == 'text' and isinstance(part.get('text'), str):
            out.append(part['text'])
        elif kind == 'data':
            out.append(_text(part.get('data')))
        elif kind == 'file':
            file = part.get('file') if isinstance(part.get('file'), dict) else {}
            out.append(f"File: {_text(file.get('name') or file.get('uri'))}")
    return '\n'.join(p for p in out if p)


def a2a_message(message: dict) -> str:
    """One A2A message with its artifacts (class 5). The verdict is not
    rendered: retrieval reads it from Core when a query runs (Knowledge
    Retrieval REQ-05)."""
    text = f"A2A {_text(message.get('role')) or 'message'} ({_text(message.get('direction'))}):\n"
    text += _parts(message.get('parts'))
    for artifact in message.get('artifacts') or []:
        body = artifact.get('artifact') if isinstance(artifact.get('artifact'), dict) else {}
        rendered = _parts(body.get('parts'))
        if rendered:
            text += f"\nArtifact {_text(body.get('name')) or ''}:\n{rendered}"
    return text if _parts(message.get('parts')) or '\nArtifact' in text else ''
