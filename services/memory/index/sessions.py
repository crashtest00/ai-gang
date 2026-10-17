"""A session transcript cut into turns (Corpus Indexing REQ-10).

Every fact about the harness's records comes from ``archive.harness`` (the
session record's one module, Durable Agent Session Record REQ-05; Corpus
REQ-01): which lines are conversational (``classify_line``), runs' end
records (H9), a response's ``message.id`` and content blocks (H14), tool
calls and results (H11), and a subagent's starting tool call (H13). Only
conversational records are read at all, so a bookkeeping, unknown or
unparseable record never produces a unit.

**The turn** (recorded at build). A turn is one model response, the
``assistant`` records sharing one ``message.id``, with the results of the
tool calls it made, attached by ``tool_use_id``. A prompt (a ``user``
record carrying text rather than tool results) leads the turn that answers
it. Records are visited by following ``parentUuid`` from each root, parent
before child and siblings in file order, so a branch of the tree is walked
as its own chain rather than interleaved by file position. ``attachment``
and ``system`` records are the harness's context, not the agent's step. They
are walked through, but not rendered. A prompt that no response answers
yet belongs to no turn.

**Rendering** (recorded at build). Each record of a turn renders, in walk
order, to lines: ``User: <prompt>``, ``Reasoning: <thinking>``,
``Assistant: <text>``, ``Tool call <name> (<id>): <input as JSON>`` and
``Tool result <name> (<id>): <text>``. Records are separated by a blank
line.

**The stub** (recorded at build). A tool result, or a tool call's input,
longer than STUB_THRESHOLD_CHARS characters renders as a stub naming the
tool, the size and the archive line, not as its text.

**The split rule** (recorded at build). A turn rendering longer than
TURN_UNIT_MAX_CHARS is cut by ``index.chunking.cut``: whole records while
they fit, then line by line, then at whitespace. Each part's reference
names the archive lines of the records it holds (``lines``,
``lineStart``/``lineEnd``), the lines of the whole turn in walk order
(``turnLines``) and its ``offset``/``end`` in the turn's rendering, which
``render`` of the records on those lines of the archive copy, in that
order, reproduces.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from archive import harness

from .chunking import cut

STUB_THRESHOLD_CHARS = 2000
TURN_UNIT_MAX_CHARS = 4000


@dataclass
class Turn:
    key: str
    records: list = field(default_factory=list)  # [(line_no, record)] in walk order

    @property
    def lines(self) -> list[int]:
        """The turn's archive lines in walk order."""
        return [ln for ln, _ in self.records]


@dataclass
class TurnUnit:
    turn: str
    text: str
    lines: list[int]
    turn_lines: list[int]
    offset: int
    end: int
    written_at: str | None


def complete_lines(data: bytes) -> list[tuple[int, bytes]]:
    """Each complete line (1-based number, bytes); a partial last line is
    left for the next read."""
    pieces = data.split(b'\n')
    return [(i + 1, line) for i, line in enumerate(pieces[:-1])]


def conversational(data: bytes) -> list[tuple[int, dict]]:
    out = []
    for number, raw in complete_lines(data):
        classified = harness.classify_line(raw)
        if classified.cls == harness.CONVERSATIONAL:
            out.append((number, classified.record))
    return out


def last_end_line(data: bytes) -> int:
    """The line of the transcript's last end record (H9), or 0: every turn
    at or before it belongs to a run that is over."""
    last = 0
    for number, raw in complete_lines(data):
        classified = harness.classify_line(raw)
        if classified.record is not None and harness.is_end_record(classified.record):
            last = number
    return last


def tool_use_line(data: bytes, tool_use_id: str) -> int | None:
    """The line of the ``assistant`` record holding the tool call."""
    for number, record in conversational(data):
        if any(tid == tool_use_id for tid, _, _ in harness.tool_uses(record)):
            return number
    return None


def turns(records: list[tuple[int, dict]]) -> list[Turn]:
    by_uuid = {}
    children: dict = {}
    for number, record in records:
        by_uuid.setdefault(record['uuid'], (number, record))
    roots = []
    for number, record in records:
        parent = record.get('parentUuid')
        if parent in by_uuid and parent != record['uuid']:
            children.setdefault(parent, []).append(record['uuid'])
        else:
            roots.append(record['uuid'])

    found: dict[str, Turn] = {}
    order: list[str] = []
    tool_owner: dict[str, str] = {}
    seen = set()
    stack = [(uid, None, ()) for uid in reversed(roots)]
    while stack:
        uid, inherited, lead = stack.pop()
        if uid in seen:
            continue
        seen.add(uid)
        number, record = by_uuid[uid]
        rtype = record.get('type')
        turn_key, next_lead = inherited, lead
        if rtype == 'assistant':
            turn_key = harness.message_id(record) or uid
            if turn_key not in found:
                found[turn_key] = Turn(turn_key)
                order.append(turn_key)
            if turn_key != inherited or lead:
                found[turn_key].records.extend(lead)
            found[turn_key].records.append((number, record))
            for tid, _, _ in harness.tool_uses(record):
                tool_owner[tid] = turn_key
            next_lead = ()
        elif rtype == 'user' and harness.tool_results(record):
            owner = next((tool_owner[t] for t, _ in harness.tool_results(record) if t in tool_owner), None)
            if owner is not None:
                found[owner].records.append((number, record))
                turn_key = owner
        elif rtype == 'user' and (harness.user_prompt_text(record) or '').strip():
            next_lead = lead + ((number, record),)
        for child in reversed(children.get(uid, [])):
            stack.append((child, turn_key, next_lead))
    return [found[k] for k in order]


def _clip(label: str, body: str, line: int) -> str:
    if len(body) > STUB_THRESHOLD_CHARS:
        return f'{label}: [stub: {len(body)} characters at archive line {line}, not indexed in full]'
    return f'{label}: {body}'


def render(turn: Turn) -> tuple[str, list[tuple[int, int, int]]]:
    """The turn's text and, per rendered record, ``(line, start, end)``."""
    names = {}
    for _, record in turn.records:
        for _, _, tid, name, _ in harness.assistant_blocks(record):
            if tid:
                names[tid] = name or 'tool'
    text, spans = '', []
    for number, record in turn.records:
        rendered = []
        if record.get('type') == 'assistant':
            for kind, body, tid, name, tool_input in harness.assistant_blocks(record):
                if kind == 'thinking' and body.strip():
                    rendered.append(f'Reasoning: {body}')
                elif kind == 'text' and body.strip():
                    rendered.append(f'Assistant: {body}')
                elif kind == 'tool_use':
                    payload = json.dumps(tool_input, sort_keys=True, ensure_ascii=False)
                    rendered.append(_clip(f'Tool call {name or "tool"} ({tid})', payload, number))
        elif harness.tool_results(record):
            for tid, body in harness.tool_results(record):
                rendered.append(_clip(f'Tool result {names.get(tid, "tool")} ({tid})', body, number))
        else:
            rendered.append(f'User: {harness.user_prompt_text(record)}')
        if not rendered:
            continue
        piece = '\n'.join(rendered) + '\n'
        if text:
            text += '\n'
        spans.append((number, len(text), len(text) + len(piece)))
        text += piece
    return text, spans


def turn_units(data: bytes, *, upto_line: int | None) -> list[TurnUnit]:
    """The units of every turn in ``data`` whose records all lie at or
    before ``upto_line`` (None: every turn)."""
    out = []
    for turn in turns(conversational(data)):
        lines = turn.lines
        if upto_line is not None and max(lines) > upto_line:
            continue
        text, spans = render(turn)
        written_at = harness.record_timestamp(turn.records[0][1]) if turn.records else None
        for piece in cut(text, TURN_UNIT_MAX_CHARS):
            used = sorted({ln for ln, s, e in spans if s < piece.end and e > piece.offset})
            if not used:
                continue
            out.append(TurnUnit(turn=turn.key, text=text[piece.offset:piece.end], lines=used, turn_lines=lines,
                                offset=piece.offset, end=piece.end, written_at=written_at))
    return out

