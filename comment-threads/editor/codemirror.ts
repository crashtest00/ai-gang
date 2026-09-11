/**
 * Editor-integration layer. NOT part of the docs-service scope, and NOT
 * exported from the module barrel -- it is the only file here that needs an
 * editor, and importing the barrel must not drag CodeMirror in.
 *
 * Requires @codemirror/state, which is not installed in this repo. Nothing
 * builds this today; it is committed so the CodeMirror + Tauri desktop work has
 * something to start from rather than reconstructing it.
 *
 * Two things to know before using it:
 *
 * 1. Marker placement is NOT decided here. `safeMarkerPosition` in
 *    ../positions.ts is normative on every surface. This file previously had a
 *    second implementation using the Lezer syntax tree, which was more accurate
 *    but editor-only; shipping both would mean the same document gets different
 *    marker placement depending on which surface wrote it. The syntax-tree
 *    version is gone and this delegates.
 *
 * 2. It contains deletion operations. The docs-service write path must not
 *    expose deletion, and the module's document-level API deliberately has no
 *    delete. An editor is a different case -- a person removing their own
 *    comment in the desktop app is an explicit human action -- but do not wire
 *    these into anything service-side.
 */
import type { ChangeSpec, EditorState } from '@codemirror/state'
import type { CommentMarker } from '../types'
import { appendThreadBlock, formatCommentMarker, parseCommentMarkers } from '../markers'
import { safeMarkerPosition } from '../positions'

export function commentThreadCreationChanges (
  state: EditorState,
  position: number,
  marker: string,
  block: string
): ChangeSpec {
  const doc = state.sliceDoc()
  const markerPosition = safeCommentMarkerPosition(state, position)
  const markerLine = state.doc.lineAt(markerPosition)
  const markerInsert = markerPosition === markerLine.from && markerLine.length === 0
    ? `${marker}\n`
    : marker

  return markerPosition === state.doc.length
    ? { from: markerPosition, insert: markerInsert + appendThreadBlock(doc + markerInsert, block) }
    : [
      { from: markerPosition, insert: markerInsert },
      { from: state.doc.length, insert: appendThreadBlock(doc, block) }
    ]
}

export function provisionalCommentMarkerChange (
  state: EditorState,
  position: number,
  marker: string
): { from: number, insert: string } {
  const markerPosition = safeCommentMarkerPosition(state, position)
  const markerLine = state.doc.lineAt(markerPosition)
  const insert = markerPosition === markerLine.from && markerLine.length === 0
    ? `${marker}\n`
    : marker

  return { from: markerPosition, insert }
}

export function provisionalCommentMarkerRemoval (
  state: EditorState,
  id: string
): { from: number, to: number, insert: string }|undefined {
  const markers = parseCommentMarkers(state.sliceDoc()).get(id)
  if (markers?.length !== 1) {
    return undefined
  }

  const marker = markers[0]
  const removeTrailingNewline = marker.from === state.doc.lineAt(marker.from).from &&
    marker.to < state.doc.length &&
    state.sliceDoc(marker.to, marker.to + 1) === '\n'

  return {
    from: marker.from,
    to: marker.to + (removeTrailingNewline ? 1 : 0),
    insert: ''
  }
}

export function commentThreadResolutionChanges (
  thread: { id: string, markers: CommentMarker[], from: number, to: number },
  block: string
): ChangeSpec|undefined {
  if (thread.markers.length !== 1) {
    return undefined
  }

  return [
    {
      from: thread.markers[0].from,
      to: thread.markers[0].to,
      insert: formatCommentMarker(thread.id, 'resolved')
    },
    { from: thread.from, to: thread.to, insert: block }
  ]
}

export function commentThreadDeletionChanges (
  state: EditorState,
  thread: { markers: CommentMarker[], from: number, to: number }
): ChangeSpec|undefined {
  if (thread.markers.length !== 1) {
    return undefined
  }

  const blockTo = thread.to < state.doc.length && state.sliceDoc(thread.to, thread.to + 1) === '\n'
    ? thread.to + 1
    : thread.to

  return [
    { from: thread.markers[0].from, to: thread.markers[0].to, insert: '' },
    { from: thread.from, to: blockTo, insert: '' }
  ]
}

/**
 * Delegates to the normative rule in ../positions.ts. Kept as a named function
 * so call sites read the same as before, and so there is exactly one place to
 * change if the bridge ever needs to do more than hand over the document text.
 */
export function safeCommentMarkerPosition (state: EditorState, position: number): number {
  return safeMarkerPosition(state.sliceDoc(), position)
}
