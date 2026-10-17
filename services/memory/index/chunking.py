"""Cutting a text into units that each resolve to a location in it
(Corpus Indexing §4, "Chunking is an implementation choice; provenance is
not").

A unit's text is always an exact slice ``text[offset:end]`` of the text it
was cut from, so its reference resolves by slicing the same text again; its
line range is the 1-based, inclusive range of lines that slice touches.

Recorded at build: a unit is at most UNIT_MAX_CHARS characters. Paragraphs
(blocks separated by a blank line, and a Markdown heading always starting a
block) are packed whole while they fit; a longer block is cut at line
boundaries, and a longer line at the last whitespace before the limit (or at
the limit when there is none). A slice holding only whitespace is no unit.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass

UNIT_MAX_CHARS = 2000


@dataclass(frozen=True)
class Cut:
    offset: int
    end: int
    line_start: int
    line_end: int


def _line_spans(text: str) -> list[tuple[int, int]]:
    spans, start = [], 0
    while start < len(text):
        nl = text.find('\n', start)
        end = len(text) if nl < 0 else nl + 1
        spans.append((start, end))
        start = end
    return spans


def _blocks(text: str, lines: list[tuple[int, int]]) -> list[tuple[int, int]]:
    blocks: list[list[int]] = []
    current: list[int] | None = None
    for start, end in lines:
        line = text[start:end]
        blank = not line.strip()
        heading = line.lstrip().startswith('#')
        if current is None or (heading and current[1] > current[0] and text[current[0]:current[1]].strip()):
            current = [start, end]
            blocks.append(current)
        else:
            current[1] = end
        if blank:
            current = None
    return [(b[0], b[1]) for b in blocks]


def _split_long(text: str, start: int, end: int, limit: int) -> list[tuple[int, int]]:
    out = []
    while end - start > limit:
        window = text[start:start + limit]
        cut = max(window.rfind('\n'), window.rfind(' '), window.rfind('\t'))
        stop = start + (cut + 1 if cut > 0 else limit)
        out.append((start, stop))
        start = stop
    if end > start:
        out.append((start, end))
    return out


def cut(text: str, limit: int = UNIT_MAX_CHARS) -> list[Cut]:
    """The units of ``text``, in order."""
    if not text:
        return []
    lines = _line_spans(text)
    line_starts = [s for s, _ in lines]

    pieces: list[tuple[int, int]] = []
    for b_start, b_end in _blocks(text, lines):
        if b_end - b_start <= limit:
            pieces.append((b_start, b_end))
            continue
        for l_start, l_end in lines[bisect.bisect_right(line_starts, b_start) - 1:]:
            if l_start >= b_end:
                break
            pieces.extend(_split_long(text, l_start, l_end, limit))

    chunks: list[tuple[int, int]] = []
    for start, end in pieces:
        if chunks and end - chunks[-1][0] <= limit:
            chunks[-1] = (chunks[-1][0], end)
        else:
            chunks.append((start, end))

    out = []
    for start, end in chunks:
        if not text[start:end].strip():
            continue
        line_start = bisect.bisect_right(line_starts, start)
        line_end = bisect.bisect_right(line_starts, end - 1)
        out.append(Cut(start, end, line_start, line_end))
    return out
