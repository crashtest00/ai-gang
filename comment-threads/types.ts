export type CommentThreadStatus = 'open'|'resolved'
/**
 * Open vocabulary. AI Gang uses per-agent identities, so this is deliberately
 * not a fixed set. The only constraint is imposed by the message header syntax:
 * an author may contain neither '|' nor ']'.
 */
export type CommentThreadAuthor = string

export interface CommentMessage {
  author: CommentThreadAuthor
  timestamp: string
  body: string
}

export interface CommentThread {
  id: string
  status: CommentThreadStatus
  messages: CommentMessage[]
  markers: CommentMarker[]
  from: number
  to: number
  line: number
}

export interface CommentMarker {
  from: number
  to: number
  status: CommentThreadStatus
}

export interface CommentThreadDraft {
  id: string
  body: string
  position: number
  documentPath: string
}

export type SerializableCommentThread = Omit<CommentThread, 'markers'|'from'|'to'|'line'>
