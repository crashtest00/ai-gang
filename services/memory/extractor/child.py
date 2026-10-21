"""One file's extraction, in a process of its own (REQ-05, REQ-06).

    python -m extractor.child <work directory>

Reads ``content`` and ``meta.json`` from the work directory and writes
``out.json`` there: ``{"outcome": "extracted", "format", "text"}`` or
``{"outcome": "failed", "format", "reason"}``. A parser exception is a
failure with its reason, never empty text. A crash this process cannot
catch (a signal, a resource limit) leaves no ``out.json``, and the parent
(``extractor.__main__``) records the crash instead.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from extractor.formats import Unsupported, decide_format, extract


def run(work: Path) -> dict:
    meta = json.loads((work / 'meta.json').read_text())
    data = (work / 'content').read_bytes()
    filename = meta.get('originalFilename') or None
    fmt = None
    try:
        fmt, _ = decide_format(data, filename)
        result = extract(data, filename)
    except Exception as exc:  # noqa: BLE001 - REQ-06: every parser defect is a recorded failure
        return {'outcome': 'failed', 'format': fmt,
                'reason': f'extraction failed: {type(exc).__name__}: {exc}'[:2000]}
    if isinstance(result, Unsupported):
        return {'outcome': 'failed', 'format': result.format, 'reason': result.reason}
    # PostgreSQL text cannot hold NUL, and no reader wants one; a lone
    # surrogate (pypdf decodes CMaps with surrogatepass) cannot be encoded
    # as UTF-8 at all, so it becomes "?".
    text = result.text.replace('\x00', '').encode('utf-8', 'replace').decode('utf-8')
    return {'outcome': 'extracted', 'format': result.format, 'text': text}


def main(argv: list[str]) -> int:
    work = Path(argv[1])
    outcome = run(work)
    tmp = work / 'out.json.tmp'
    tmp.write_text(json.dumps(outcome))
    os.replace(tmp, work / 'out.json')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
