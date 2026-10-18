"""The agent harness's on-disk session format — every assumption Memory makes
about it, in one module.

This is the harness-specific half of Memory's archive (Durable Agent Session
Record REQ-05: "The list of known types and the envelope MUST live in one
module"). Everything else in Memory — the copier, the manifest, the reads —
asks this module what a path or a record is and never inspects a harness
record itself. A harness that changes its format, or a second harness,
costs this one file. v6.1's indexer classifies through `classify_line`
rather than a copy of its rules (Corpus Indexing REQ-01).

The facts below come from the harness binding (Management repository,
`strategy/bindings/claude-code.md`, "Session data"). Each is an assumption:
if the harness changes, it is here that Memory goes wrong.

  H1  Layout. The harness volume holds one subdirectory per project; under
      it, one directory per encoded working directory (`-workspace` in a
      project container), holding `<sessionId>.jsonl` and a sidecar
      directory `<sessionId>/` (binding, "Location", "Files").
  H2  A session's files are its transcript and everything under its
      sidecar: `subagents/agent-*.jsonl` and their `.meta.json` /
      `.prefix.json`, and `tool-results/` (binding, "Files").
  H3  Notes are files directly in `memory/` beside the transcripts whose
      name is `MEMORY.md` or ends in `.md` (a leading dot does not matter); anything else there (the agent
      tools' temporary files, e.g. `MEMORY.mdhfCMLl`,
      `x.md.tmp.448.1e0a4313e325`) is not a note (binding, "Notes").
  H4  Records are one JSON object per line in `.jsonl` files (binding,
      "Files").
  H5  Conversational types are `user`, `assistant`, `attachment`, `system`,
      carrying the envelope `type`, `uuid`, `parentUuid`, `sessionId`,
      `timestamp`, `version` (specification REQ-05's table; envelope
      confirmed by the orchestrator against 2.1.296 project-container
      transcripts).
  H6  Known bookkeeping types are the binding's "Record types" list other
      than the conversational four.
  H7  Bookkeeping records may carry no `version` (2.1.296: `cost-state`,
      `last-prompt`, `atis-latch`, `mode`, `queue-operation`); a record's
      version is the one it carries, else the last one named before it in
      the same file (specification REQ-05).
  H8  The prompt record is a `queue-operation` whose `operation` is
      `enqueue`, except one whose `content` begins `<task-notification>` or
      `<agent-message from=`; a `--resume`d `--print` run writes one too
      (binding, "Prompt record"; resume confirmed by the orchestrator,
      run 20261010-131150).
  H9  The end record is `cost-state`: one per completed run, none for a
      killed run (binding, "End record"). Only the session's own
      transcript opens and ends runs (specification REQ-05).
  H10 `pr-link` records carry `prRepository`, `prNumber`, `prUrl`,
      `timestamp` (binding, "pr-link").
  H11 A shell command is a `tool_use` content block named `Bash` with
      `input.command`, inside an `assistant` record's `message.content`; its
      output is a `tool_result` block naming it by `tool_use_id`, inside a
      `user` record's `message.content` (binding, "Tool calls").
  H12 git prints `[<branch> <hash>] <subject>` on its own line for a commit
      (specification REQ-07); `<branch>` may be `main (root-commit)` or
      `detached HEAD`, so the hash is the last word before `]`.
  H13 A subagent transcript `<sid>/subagents/agent-<x>.jsonl` has a sidecar
      `agent-<x>.meta.json` beside it: one JSON object whose `toolUseId`
      names the `tool_use` block, in the parent session's transcript, that
      started the subagent (Corpus Indexing REQ-10; build brief DR-5:
      observed on the management host, harness 2.1.291, October 14 and 17,
      2026; not yet in the binding).
  H14 One model response is written as several `assistant` records, one per
      content block, sharing `message.id`. Block types are `thinking`
      (text in `thinking`), `text` (`text`) and `tool_use` (`id`, `name`,
      `input`). A `user` record carries a prompt as a string
      `message.content` or as `text` blocks, and tool results as
      `tool_result` blocks (H11). Corpus Indexing REQ-10 cuts turns by this;
      observed on the same host and version as H13.
"""

import json
import re

# --- H1 / H2 / H3: paths ---------------------------------------------------

NOTES_DIR = 'memory'
SUBAGENTS_DIR = 'subagents'
TRANSCRIPT_SUFFIX = '.jsonl'
NOTE_SUFFIX = '.md'

KIND_TRANSCRIPT = 'transcript'
KIND_SIDECAR = 'sidecar'
KIND_NOTE = 'note'


def classify_path(parts):
    """What a path on the harness volume is.

    `parts` is the path relative to the volume root, split into components:
    `(project, encoded_cwd, ...)`. Returns `(kind, session_id)` — session_id
    is None for a note — or None for a path Memory does not copy.
    """
    if len(parts) < 3:
        return None
    rest = parts[2:]
    name = rest[0]
    if len(rest) == 1:
        if name.endswith(TRANSCRIPT_SUFFIX) and len(name) > len(TRANSCRIPT_SUFFIX):
            return (KIND_TRANSCRIPT, name[: -len(TRANSCRIPT_SUFFIX)])
        return None
    if name == NOTES_DIR:
        if len(rest) == 2 and is_note_name(rest[1]):
            return (KIND_NOTE, None)
        return None
    return (KIND_SIDECAR, name)


def is_note_name(name):
    return name.endswith(NOTE_SUFFIX)


def is_line_file(parts):
    """A file copied by complete lines while its session is active (REQ-03)."""
    return parts[-1].endswith(TRANSCRIPT_SUFFIX)


def is_classified_file(parts):
    """A file whose lines REQ-05 classifies: the transcript, or a subagent
    transcript (`<sid>/subagents/*.jsonl`)."""
    kind = classify_path(parts)
    if kind is None or not parts[-1].endswith(TRANSCRIPT_SUFFIX):
        return False
    if kind[0] == KIND_TRANSCRIPT:
        return True
    return kind[0] == KIND_SIDECAR and len(parts) == 5 and parts[3] == SUBAGENTS_DIR


def is_session_transcript(parts):
    kind = classify_path(parts)
    return kind is not None and kind[0] == KIND_TRANSCRIPT


def transcript_parts(project, encoded_cwd, session_id):
    return (project, encoded_cwd, session_id + TRANSCRIPT_SUFFIX)


# --- H4–H7: classification ---------------------------------------------------

CONVERSATIONAL = 'conversational'
BOOKKEEPING = 'bookkeeping'
UNKNOWN = 'unknown'
UNPARSEABLE = 'unparseable'
CLASSES = (CONVERSATIONAL, BOOKKEEPING, UNKNOWN, UNPARSEABLE)

# The version a record is counted against when no `version` precedes it.
UNKNOWN_VERSION = 'unknown'

CONVERSATIONAL_TYPES = frozenset({'user', 'assistant', 'attachment', 'system'})
ENVELOPE_FIELDS = ('type', 'uuid', 'parentUuid', 'sessionId', 'timestamp', 'version')
BOOKKEEPING_TYPES = frozenset({
    'queue-operation', 'last-prompt', 'cost-state', 'atis-latch', 'mode',
    'pr-link', 'agent-name', 'ai-title', 'bridge-session', 'custom-title',
    'file-history-delta', 'file-history-snapshot', 'permission-mode',
})


class Classified:
    """One line's classification. Only CONVERSATIONAL is usable for
    indexing (REQ-05's table). `record` is the parsed object, or None for
    an unparseable line; `version` is the `version` the record itself
    carries, or None."""

    __slots__ = ('cls', 'record', 'version')

    def __init__(self, cls, record, version):
        self.cls = cls
        self.record = record
        self.version = version


def classify_line(line):
    """Classify one line (bytes, without its newline) of a `.jsonl` file."""
    try:
        record = json.loads(line)
    except (ValueError, UnicodeDecodeError):
        return Classified(UNPARSEABLE, None, None)
    if not isinstance(record, dict):
        return Classified(UNKNOWN, record, None)
    version = record.get('version')
    if not isinstance(version, str) or not version:
        version = None
    rtype = record.get('type')
    if rtype in CONVERSATIONAL_TYPES:
        if all(field in record for field in ENVELOPE_FIELDS) and version is not None:
            return Classified(CONVERSATIONAL, record, version)
        return Classified(UNKNOWN, record, version)
    if rtype in BOOKKEEPING_TYPES:
        return Classified(BOOKKEEPING, record, version)
    return Classified(UNKNOWN, record, version)


# --- H8 / H9: runs ----------------------------------------------------------

NOT_PROMPT_PREFIXES = ('<task-notification>', '<agent-message from=')


def is_prompt_record(record):
    if not isinstance(record, dict) or record.get('type') != 'queue-operation':
        return False
    if record.get('operation') != 'enqueue':
        return False
    content = record.get('content')
    if isinstance(content, str) and content.startswith(NOT_PROMPT_PREFIXES):
        return False
    return True


def is_end_record(record):
    return isinstance(record, dict) and record.get('type') == 'cost-state'


# --- H10: pull requests -----------------------------------------------------

def pull_request(record):
    """`(repository, number, url, timestamp)` from a `pr-link` record, or None."""
    if not isinstance(record, dict) or record.get('type') != 'pr-link':
        return None
    repository = record.get('prRepository')
    number = record.get('prNumber')
    if isinstance(number, str) and number.isdigit():
        number = int(number)
    if not isinstance(repository, str) or not isinstance(number, int) or isinstance(number, bool):
        return None
    url = record.get('prUrl') if isinstance(record.get('prUrl'), str) else ''
    return (repository, number, url, record_timestamp(record))


def record_timestamp(record):
    ts = record.get('timestamp') if isinstance(record, dict) else None
    return ts if isinstance(ts, str) else None


# --- H11 / H12: commands and git's commit lines -----------------------------

SHELL_TOOL = 'Bash'
_GIT = re.compile(r'(?<![\w.-])git(?![\w.-])')
_SQUASH = re.compile(r'git\s+merge\s+--squash\b')
_COMMIT_LINE = re.compile(r'^\[[^\]\n]*?\s([0-9a-f]{7,40})\][ \t]', re.MULTILINE)


def _content_blocks(record):
    message = record.get('message') if isinstance(record, dict) else None
    content = message.get('content') if isinstance(message, dict) else None
    return content if isinstance(content, list) else []


def tool_uses(record):
    """`(tool_use_id, name, command)` for each tool call an `assistant`
    record makes; command is None for a tool other than the shell."""
    if not isinstance(record, dict) or record.get('type') != 'assistant':
        return []
    out = []
    for block in _content_blocks(record):
        if isinstance(block, dict) and block.get('type') == 'tool_use' and isinstance(block.get('id'), str):
            command = None
            if block.get('name') == SHELL_TOOL and isinstance(block.get('input'), dict):
                cmd = block['input'].get('command')
                command = cmd if isinstance(cmd, str) else None
            out.append((block['id'], block.get('name'), command))
    return out


def tool_results(record):
    """`(tool_use_id, text)` for each tool result a `user` record carries."""
    if not isinstance(record, dict) or record.get('type') != 'user':
        return []
    out = []
    for block in _content_blocks(record):
        if not (isinstance(block, dict) and block.get('type') == 'tool_result'):
            continue
        tid = block.get('tool_use_id')
        if not isinstance(tid, str):
            continue
        content = block.get('content')
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = '\n'.join(part.get('text', '') for part in content
                             if isinstance(part, dict) and isinstance(part.get('text'), str))
        else:
            text = ''
        out.append((tid, text))
    return out


def is_git_command(command):
    """A shell command that runs git — the only kind whose output can carry
    git's own `[<branch> <hash>]` line (REQ-07: "git's output")."""
    return command is not None and _GIT.search(command) is not None


def is_squash_command(command):
    return command is not None and _SQUASH.search(command) is not None


def commit_hashes(text):
    """The abbreviated hashes of git's `[<branch> <hash>]` lines, in order."""
    return [m.group(1) for m in _COMMIT_LINE.finditer(text)]


# --- H13: a subagent's starting tool call ----------------------------------

META_SUFFIX = '.meta.json'


def is_subagent_transcript(archive_path):
    """`<project>/<cwd>/<sid>/subagents/<name>.jsonl`, as a '/'-joined path."""
    parts = tuple(archive_path.split('/'))
    return is_classified_file(parts) and len(parts) == 5 and parts[3] == SUBAGENTS_DIR


def subagent_meta_path(archive_path):
    """The `.meta.json` beside a subagent transcript."""
    return archive_path[: -len(TRANSCRIPT_SUFFIX)] + META_SUFFIX


def subagent_tool_use_id(meta_bytes):
    """The `toolUseId` a subagent's `.meta.json` names, or None."""
    try:
        meta = json.loads(meta_bytes)
    except (ValueError, UnicodeDecodeError):
        return None
    value = meta.get('toolUseId') if isinstance(meta, dict) else None
    return value if isinstance(value, str) and value else None


# --- H14: turns -------------------------------------------------------------

def message_id(record):
    """The model response an `assistant` record belongs to, or None."""
    message = record.get('message') if isinstance(record, dict) else None
    value = message.get('id') if isinstance(message, dict) else None
    return value if isinstance(value, str) and value else None


def assistant_blocks(record):
    """`(kind, text, tool_use_id, tool_name, tool_input)` for each block of an
    `assistant` record; kind is `thinking`, `text` or `tool_use`."""
    if not isinstance(record, dict) or record.get('type') != 'assistant':
        return []
    out = []
    for block in _content_blocks(record):
        if not isinstance(block, dict):
            continue
        kind = block.get('type')
        if kind == 'thinking' and isinstance(block.get('thinking'), str):
            out.append(('thinking', block['thinking'], None, None, None))
        elif kind == 'text' and isinstance(block.get('text'), str):
            out.append(('text', block['text'], None, None, None))
        elif kind == 'tool_use' and isinstance(block.get('id'), str):
            out.append(('tool_use', None, block['id'], block.get('name'), block.get('input')))
    return out


def user_prompt_text(record):
    """The prompt text a `user` record carries (not its tool results), or None."""
    if not isinstance(record, dict) or record.get('type') != 'user':
        return None
    message = record.get('message')
    content = message.get('content') if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    texts = [b['text'] for b in _content_blocks(record)
             if isinstance(b, dict) and b.get('type') == 'text' and isinstance(b.get('text'), str)]
    return '\n'.join(texts) if texts else None
