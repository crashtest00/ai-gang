"""Format decision and text extraction — artifact-text-extraction.md REQ-06.

V4 records no file type: stored files carry no extension and retrieval
returns every file as ``application/octet-stream``. So the format is decided
here, from the bytes first and the recorded original filename second, and
the decided format is returned for the extraction record to carry.

Declared formats (the minimum REQ-06 names, no more):

    application/pdf                                                  PDF
    application/vnd.openxmlformats-officedocument.wordprocessingml.document
                                                                     Word (.docx)
    text/plain                                                       plain text
    text/markdown                                                    Markdown
    text/html                                                        HTML
    text/csv                                                         CSV
    text/tab-separated-values                                        TSV

Everything else is an undeclared format and comes back as an
``Unsupported`` result naming what was recognised (an image, a legacy
Office binary, an archive, unrecognised binary content). Nothing here
touches the network, and no parser needs data files fetched at run time
(brief I-08): pypdf ships its tables inside the wheel, and the rest is the
standard library.

This module runs only inside the extractor's per-file child process
(``extractor.child``), never in a process holding a database connection
(REQ-05).
"""

from __future__ import annotations

import codecs
import csv
import io
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Optional
from xml.etree import ElementTree

PDF = 'application/pdf'
DOCX = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
TEXT = 'text/plain'
MARKDOWN = 'text/markdown'
HTML = 'text/html'
CSV = 'text/csv'
TSV = 'text/tab-separated-values'

DECLARED_FORMATS = (PDF, DOCX, TEXT, MARKDOWN, HTML, CSV, TSV)

_EXTENSIONS = {
    '.md': MARKDOWN, '.markdown': MARKDOWN,
    '.html': HTML, '.htm': HTML, '.xhtml': HTML,
    '.csv': CSV,
    '.tsv': TSV, '.tab': TSV,
    '.txt': TEXT, '.text': TEXT,
}

# Magic numbers of formats recognised only to be refused by name.
_IMAGE_MAGIC = (
    (b'\x89PNG\r\n\x1a\n', 'image/png'),
    (b'\xff\xd8\xff', 'image/jpeg'),
    (b'GIF87a', 'image/gif'),
    (b'GIF89a', 'image/gif'),
    (b'II*\x00', 'image/tiff'),
    (b'MM\x00*', 'image/tiff'),
)
_OLE2_MAGIC = b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'
_ZIP_MAGIC = b'PK\x03\x04'


@dataclass
class Extracted:
    format: str
    text: str


@dataclass
class Unsupported:
    format: str
    reason: str


def _extension(original_filename: Optional[str]) -> str:
    name = (original_filename or '').strip().lower()
    dot = name.rfind('.')
    return name[dot:] if dot > 0 else ''


def _image_format(head: bytes) -> Optional[str]:
    for magic, mime in _IMAGE_MAGIC:
        if head.startswith(magic):
            return mime
    if head[:4] == b'RIFF' and head[8:12] == b'WEBP':
        return 'image/webp'
    if head[4:8] == b'ftyp' and head[8:12] in (b'heic', b'heix', b'avif', b'mif1'):
        return 'image/heif'
    return None


def _decode_text(data: bytes) -> Optional[tuple[str, str]]:
    """Decode bytes that are text, or return None for binary content.

    UTF-8 (with or without a byte-order mark) and UTF-16 with a BOM are
    tried first; a file with no NUL byte that is not UTF-8 is read as
    Windows-1252 only when almost nothing in it is a control character,
    so arbitrary binary is never passed off as text.
    """
    if data.startswith(codecs.BOM_UTF8):
        try:
            return data[len(codecs.BOM_UTF8):].decode('utf-8'), 'utf-8'
        except UnicodeDecodeError:
            return None
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        try:
            return data.decode('utf-16'), 'utf-16'
        except UnicodeDecodeError:
            return None
    if b'\x00' in data:
        return None
    try:
        return data.decode('utf-8'), 'utf-8'
    except UnicodeDecodeError:
        pass
    try:
        text = data.decode('cp1252')
    except UnicodeDecodeError:
        return None
    controls = sum(1 for ch in text if ord(ch) < 32 and ch not in '\t\n\r\f')
    if text and controls / len(text) < 0.01:
        return text, 'windows-1252'
    return None


def _looks_like_html(text: str) -> bool:
    head = text.lstrip()[:512].lower()
    return head.startswith('<!doctype html') or head.startswith('<html') or (
        head.startswith('<?xml') and '<html' in head)


def _looks_delimited(text: str, delimiter: str) -> bool:
    """At least three non-empty lines, every one of them with the same
    number of fields, and more than one field."""
    lines = [ln for ln in text.splitlines() if ln.strip()][:50]
    if len(lines) < 3:
        return False
    try:
        counts = {len(row) for row in csv.reader(lines, delimiter=delimiter)}
    except csv.Error:
        return False
    return len(counts) == 1 and counts.pop() > 1


def decide_format(data: bytes, original_filename: Optional[str]) -> tuple[str, Optional[str]]:
    """Return ``(format, unsupported_reason)``. The reason is None for a
    declared format and names what was recognised otherwise."""
    head = data[:1024]
    # PDF readers accept binary junk before the header within the first
    # 1,024 bytes; a text file that merely mentions "%PDF-" is not a PDF.
    marker = head.find(b'%PDF-')
    if marker >= 0:
        prefix = head[:marker]
        if not prefix.strip() or any(b < 9 or 13 < b < 32 for b in prefix):
            return PDF, None
    image = _image_format(head)
    if image is not None:
        return image, f'unsupported format: {image} (image content is not extracted, REQ-06)'
    if head.startswith(_OLE2_MAGIC):
        return 'application/x-ole-storage', (
            'unsupported format: legacy Office binary (.doc/.xls/.ppt); Word is extracted from .docx')
    if head.startswith(_ZIP_MAGIC):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                names = set(archive.namelist())
        except zipfile.BadZipFile:
            return 'application/zip', 'unsupported format: damaged zip archive'
        if 'word/document.xml' in names:
            return DOCX, None
        if 'xl/workbook.xml' in names:
            return 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', (
                'unsupported format: spreadsheet workbook (.xlsx)')
        if 'ppt/presentation.xml' in names:
            return 'application/vnd.openxmlformats-officedocument.presentationml.presentation', (
                'unsupported format: presentation (.pptx)')
        return 'application/zip', 'unsupported format: zip archive'
    decoded = _decode_text(data)
    if decoded is None:
        return 'application/octet-stream', 'unsupported format: unrecognised binary content'
    text, _encoding = decoded
    by_name = _EXTENSIONS.get(_extension(original_filename))
    if by_name is not None:
        return by_name, None
    if _looks_like_html(text):
        return HTML, None
    if _looks_delimited(text, '\t'):
        return TSV, None
    if _looks_delimited(text, ','):
        return CSV, None
    return TEXT, None


# --- Extractors ----------------------------------------------------------

def _pdf_text(data: bytes) -> str:
    from pypdf import PdfReader  # imported here: only a PDF needs it

    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        # An empty owner password opens most "protected" PDFs; anything
        # else is a failure with its reason, not empty text.
        reader.decrypt('')
    pages = [(page.extract_text() or '') for page in reader.pages]
    return '\n\n'.join(p.strip('\n') for p in pages).strip()


_W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'


def _docx_text(data: bytes) -> str:
    """The body text of word/document.xml: runs' text, tabs and breaks,
    one line per paragraph. Streamed with iterparse; the standard library's
    expat refuses external entities and limits entity amplification."""
    out: list[str] = []
    with zipfile.ZipFile(io.BytesIO(data)) as archive, archive.open('word/document.xml') as xml:
        for event, element in ElementTree.iterparse(xml, events=('end',)):
            tag = element.tag
            if tag == f'{_W}t':
                out.append(element.text or '')
            elif tag == f'{_W}tab':
                out.append('\t')
            elif tag in (f'{_W}br', f'{_W}cr'):
                out.append('\n')
            elif tag == f'{_W}p':
                out.append('\n')
                element.clear()
    return ''.join(out).strip()


class _HTMLText(HTMLParser):
    _SKIP = {'script', 'style', 'template', 'head'}
    _BLOCK = {'p', 'div', 'br', 'li', 'tr', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'section', 'article',
              'header', 'footer', 'pre', 'blockquote', 'table', 'ul', 'ol', 'dd', 'dt', 'title', 'hr'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0
        self._title: list[str] = []
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == 'title':
            self._in_title = True
        elif tag in self._SKIP:
            self._skip += 1
        if tag in self._BLOCK:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag == 'title':
            self._in_title = False
        elif tag in self._SKIP and self._skip:
            self._skip -= 1
        if tag in self._BLOCK:
            self.parts.append('\n')

    def handle_data(self, data):
        if self._in_title:
            self._title.append(data)
        elif not self._skip:
            self.parts.append(data)


def _html_text(text: str) -> str:
    parser = _HTMLText()
    parser.feed(text)
    parser.close()
    title = ''.join(parser._title).strip()
    lines = [' '.join(line.split()) for line in ''.join(parser.parts).splitlines()]
    body = '\n'.join(line for line in lines if line)
    return f'{title}\n{body}'.strip() if title else body


def _delimited_text(text: str, delimiter: str) -> str:
    """One line per row, cells separated by a tab, quoting removed."""
    rows = csv.reader(io.StringIO(text, newline=''), delimiter=delimiter)
    return '\n'.join('\t'.join(cell.strip() for cell in row) for row in rows if any(c.strip() for c in row))


def extract(data: bytes, original_filename: Optional[str] = None):
    """Decide the format and extract its text. Returns ``Extracted`` (whose
    text may be empty: a successful extraction of an empty file) or
    ``Unsupported``. A parser defect raises; the child process records it
    as a failure with its reason."""
    fmt, unsupported = decide_format(data, original_filename)
    if unsupported is not None:
        return Unsupported(fmt, unsupported)
    if fmt == PDF:
        return Extracted(fmt, _pdf_text(data))
    if fmt == DOCX:
        return Extracted(fmt, _docx_text(data))
    text, _encoding = _decode_text(data)  # decide_format proved it decodes
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    if fmt == HTML:
        return Extracted(fmt, _html_text(text))
    if fmt == CSV:
        return Extracted(fmt, _delimited_text(text, ','))
    if fmt == TSV:
        return Extracted(fmt, _delimited_text(text, '\t'))
    return Extracted(fmt, text)
