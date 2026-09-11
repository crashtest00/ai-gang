/**
 * Document-level operations: document in, document out.
 *
 * The primitives in the other files operate on thread objects and leave document
 * assembly to the caller. That works for an editor, which does its own
 * assembly, but it puts the format's guarantees in the caller's hands: opening a
 * thread correctly is a five-step sequence, and a caller that skips the marker
 * step produces a block that is parseable but not addressable.
 *
 * These three operations hold those guarantees structurally instead:
 *
 * - `openThread` places marker and block together, running the requested offset
 *   through `safeMarkerPosition` itself, so a thread without an anchor is not
 *   constructible through this interface.
 * - `appendToThread` and `setThreadStatus` name an intent rather than taking a
 *   replacement string, so there is no way to reach deletion through them.
 *
 * There is deliberately NO document-level edit operation. The high-level surface
 * is append-only by construction; `editCommentMessage` remains available as a
 * primitive for an editor that needs it. The layering is the point, not an
 * omission.
 *
 * The primitives stay exported and unchanged. This is a layer over them.
 */
import type { CommentThread, CommentThreadStatus } from './types'
import { appendThreadBlock, formatCommentMarker } from './markers'
import { parseCommentThreads, serializeCommentThread } from './parser'
import { appendReply, createCommentThread } from './commands'
import { safeMarkerPosition } from './positions'

/**
 * Names one thread in a document. Ids are not unique -- a document can carry two
 * blocks with the same id -- so `ordinal` selects among same-id blocks in
 * document order when it has to. The mutating operations refuse an ambiguous
 * reference rather than guessing, because silently mutating the wrong one of two
 * same-id threads is the wrong-passage failure this format already risks; there
 * is no reason to add a second route to it.
 */
export interface ThreadRef {
  id: string
  ordinal?: number
}

interface DocumentEdit {
  from: number
  to: number
  insert: string
}

/** Applies edits back-to-front so earlier offsets stay valid as we go. */
function applyEdits (doc: string, edits: DocumentEdit[]): string {
  return [ ...edits ]
    .sort((a, b) => b.from - a.from)
    .reduce((result, edit) => result.slice(0, edit.from) + edit.insert + result.slice(edit.to), doc)
}

/**
 * Finds the single thread a reference names. Throws when the reference matches
 * nothing, or matches several blocks and carries no ordinal to choose between
 * them.
 */
export function resolveThreadRef (doc: string, ref: ThreadRef): CommentThread {
  const matches = parseCommentThreads(doc).filter(thread => thread.id === ref.id)

  if (matches.length === 0) {
    throw new Error(`No thread ${JSON.stringify(ref.id)} in this document.`)
  }

  if (ref.ordinal === undefined) {
    if (matches.length > 1) {
      throw new Error(
        `Ambiguous thread reference ${JSON.stringify(ref.id)}: ${matches.length} blocks carry that id. ` +
        'Supply an ordinal to choose between them.'
      )
    }
    return matches[0]
  }

  const chosen = matches[ref.ordinal]
  if (chosen === undefined) {
    throw new Error(
      `No thread ${JSON.stringify(ref.id)} at ordinal ${ref.ordinal}: only ${matches.length} block(s) carry that id.`
    )
  }

  return chosen
}

/**
 * Opens a thread anchored near `offset`, returning the new document and the
 * thread as it parses back out of it.
 *
 * The offset is a request, not an instruction: it is moved forward out of any
 * fenced code block, HTML comment, code span or link it landed inside. Callers
 * deriving an offset from a search hit or a diff should expect it to move.
 */
export function openThread (
  doc: string,
  offset: number,
  author: string,
  body = ''
): { doc: string, thread: CommentThread } {
  const created = createCommentThread(author, body)
  const at = safeMarkerPosition(doc, offset)
  const withMarker = doc.slice(0, at) + formatCommentMarker(created.id) + doc.slice(at)
  const next = `${withMarker}${appendThreadBlock(withMarker, serializeCommentThread(created))}\n`

  const thread = parseCommentThreads(next).find(candidate => candidate.id === created.id)
  if (thread === undefined) {
    throw new Error(`Internal: thread ${created.id} did not parse back out of the document it was written into.`)
  }

  return { doc: next, thread }
}

/** Appends a message to an existing thread. */
export function appendToThread (doc: string, ref: ThreadRef, author: string, body: string): string {
  const thread = resolveThreadRef(doc, ref)
  const updated = appendReply(thread, author, body)

  return applyEdits(doc, [
    { from: thread.from, to: thread.to, insert: serializeCommentThread(updated) }
  ])
}

/**
 * Sets a thread's status, updating its block and its marker glyph together so
 * the two cannot disagree. A thread whose marker is missing is still updated;
 * the orphaned state is reported by `parseCommentThreads` returning an empty
 * `markers` array, not by failing here.
 */
export function setThreadStatus (doc: string, ref: ThreadRef, status: CommentThreadStatus): string {
  const thread = resolveThreadRef(doc, ref)

  const edits: DocumentEdit[] = [
    {
      from: thread.from,
      to: thread.to,
      insert: serializeCommentThread({ id: thread.id, status, messages: thread.messages })
    }
  ]

  for (const marker of thread.markers) {
    edits.push({ from: marker.from, to: marker.to, insert: formatCommentMarker(thread.id, status) })
  }

  return applyEdits(doc, edits)
}
