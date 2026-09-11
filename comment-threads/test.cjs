const assert = require('assert')
const m = require('./dist/index.js')

// --- format round-trip -------------------------------------------------
const thread = m.createCommentThread('user', 'This paragraph needs a citation.')
const block = m.serializeCommentThread(thread)
const doc = `Some prose here ${m.formatCommentMarker(thread.id)}\n\n${block}\n`
const parsed = m.parseCommentThreads(doc)
assert.strictEqual(parsed.length, 1)
assert.strictEqual(parsed[0].id, thread.id)
assert.strictEqual(parsed[0].messages[0].body, 'This paragraph needs a citation.')
assert.strictEqual(parsed[0].markers.length, 1)

// --- reply / resolve ---------------------------------------------------
const replied = m.appendReply(parsed[0], 'user', 'Added one.')
const resolved = m.resolveThread({ ...parsed[0], messages: replied.messages })
const doc2 = m.replaceCommentThreadBlock(doc, parsed[0], m.serializeCommentThread(resolved))
const re = m.parseCommentThreads(doc2)
assert.strictEqual(re[0].status, 'resolved')
assert.strictEqual(re[0].messages.length, 2)
assert.ok(m.formatCommentMarker(re[0].id, 'resolved').includes('✅'), 'resolved glyph is U+2705')

// --- id uniqueness under a frozen clock --------------------------------
const RealDate = Date
global.Date = class extends RealDate { constructor () { super('2026-09-10T14:30:22') } }
const ids = new Set(Array.from({ length: 2000 }, () => m.createCommentThread('user', 'x').id))
global.Date = RealDate
assert.strictEqual(ids.size, 2000)

// --- position safety: fenced code --------------------------------------
const fenced = 'intro\n\n```js\nconst a = 1\n```\n\nafter\n'
const insideFence = fenced.indexOf('const a')
assert.notStrictEqual(m.safeMarkerPosition(fenced, insideFence), insideFence, 'must not stay in fence')
assert.strictEqual(m.safeMarkerPosition(fenced, insideFence), fenced.indexOf('```\n\nafter') + 4,
  'clears the closing fence line entirely, not just the delimiter')
// a marker on the delimiter's own line would stop the construct closing
{
  const at = m.safeMarkerPosition(fenced, insideFence)
  const spliced = fenced.slice(0, at) + m.formatCommentMarker('f1') + fenced.slice(at)
  assert.strictEqual(m.fencedCodeRanges(spliced).length, 1, 'fence still closes after insertion')
}
assert.strictEqual(m.fencedCodeRanges(fenced).length, 1)

// unterminated fence runs to EOF
assert.strictEqual(m.fencedCodeRanges('a\n```\nb\n').length, 1)
assert.strictEqual(m.safeMarkerPosition('a\n```\nb\n', 6), 8)

// tildes, and a longer closing fence
assert.strictEqual(m.fencedCodeRanges('~~~\nx\n~~~\n').length, 1)
assert.strictEqual(m.fencedCodeRanges('```\nx\n`````\n').length, 1)

// --- position safety: inline code + links ------------------------------
const inline = 'use `npm install` now'
const insideCode = inline.indexOf('npm')
assert.strictEqual(m.safeMarkerPosition(inline, insideCode), inline.indexOf('` now') + 1)

const link = 'see [the docs](https://example.com) here'
const insideLink = link.indexOf('example')
assert.strictEqual(m.safeMarkerPosition(link, insideLink), link.indexOf(') here') + 1)

// an existing marker is a link: never split one
const withMarker = `text [💬](#md-thread-abc) more`
const insideMarker = withMarker.indexOf('#md-thread')
assert.strictEqual(m.safeMarkerPosition(withMarker, insideMarker), withMarker.indexOf(') more') + 1)

// backticks inside a fence are not treated as inline spans
assert.strictEqual(m.inlineCodeRanges('```\na `b` c\n```\n').length, 0)

// --- safe positions are left alone -------------------------------------
const plain = 'just some prose here'
assert.strictEqual(m.safeMarkerPosition(plain, 9), 9)
assert.strictEqual(m.safeMarkerPosition(plain, 0), 0)
assert.strictEqual(m.safeMarkerPosition(plain, 999), plain.length)
assert.strictEqual(m.safeMarkerPosition(fenced, fenced.indexOf('after')), fenced.indexOf('after'))

// --- malformed blocks ignored ------------------------------------------
for (const bad of ['<!--\n@thread nope\n-->', '<!--\n@status open\n-->', '<!--\n@thread a\n@status bogus\n-->', '<!--\n@thread a\n@status open\n']) {
  assert.strictEqual(m.parseCommentThreads(bad).length, 0)
}

// --- end-to-end: agent appends a comment to a doc with a code block ----
let live = 'Intro para.\n\n```py\nx = 1\n```\n\nOutro para.\n'
const t2 = m.createCommentThread('user', 'Explain this constant.')
const pos = m.safeMarkerPosition(live, live.indexOf('x = 1'))
const mk = m.formatCommentMarker(t2.id)
live = live.slice(0, pos) + mk + live.slice(pos) 
live = live.replace(/\n*$/, '\n\n') + m.serializeCommentThread(t2) + '\n'
const found = m.parseCommentThreads(live)
assert.strictEqual(found.length, 1, 'thread readable after append')
assert.strictEqual(found[0].markers.length, 1, 'exactly one marker')
assert.ok(live.includes('x = 1\n```'), 'code block left intact')

// --- ordinal pairing under duplicate ids -------------------------------
const DUP = 'dup1'
const dupBlock = (body) => `<!--\n@thread ${DUP}\n@status open\n\n[user | ts]\n${body}\n-->`
const dupDoc = `Para one ${m.formatCommentMarker(DUP)} text.\n\n` +
  `Para two ${m.formatCommentMarker(DUP)} text.\n\n${dupBlock('FIRST')}\n\n${dupBlock('SECOND')}\n`
const dupThreads = m.parseCommentThreads(dupDoc)
const dupMarkers = m.parseCommentMarkers(dupDoc).get(DUP)
assert.strictEqual(dupThreads.length, 2, 'both blocks parsed')
assert.strictEqual(dupMarkers.length, 2, 'both markers found')
// nth block pairs with nth marker, in document order
assert.strictEqual(dupThreads[0].markers.length, 1, 'first block gets exactly one marker')
assert.strictEqual(dupThreads[1].markers.length, 1, 'second block gets exactly one marker')
assert.strictEqual(dupThreads[0].markers[0].from, dupMarkers[0].from, 'FIRST pairs with earlier marker')
assert.strictEqual(dupThreads[1].markers[0].from, dupMarkers[1].from, 'SECOND pairs with later marker')
assert.notStrictEqual(dupThreads[0].markers[0].from, dupThreads[1].markers[0].from, 'no collapse onto one marker')
// the unique-id case is unchanged by pairing
assert.strictEqual(parsed[0].markers.length, 1, 'unique id still gets its marker')

// --- surplus and orphan detection --------------------------------------
// two markers, one block: second marker is dangling
const surplus = `a ${m.formatCommentMarker(DUP)} b ${m.formatCommentMarker(DUP)} c\n\n${dupBlock('only')}\n`
assert.strictEqual(m.parseCommentThreads(surplus)[0].markers.length, 1, 'block takes its ordinal marker')
assert.strictEqual(m.danglingMarkers(surplus).get(DUP).length, 1, 'surplus marker reported dangling')
// block with no marker at all is orphaned but visible
assert.strictEqual(m.parseCommentThreads(dupBlock('lonely'))[0].markers.length, 0, 'orphan block detectable')
// marker whose block was removed is now detectable
const removed = `prose ${m.formatCommentMarker('gone')} more`
assert.strictEqual(m.parseCommentThreads(removed).length, 0, 'no block to parse')
assert.strictEqual(m.danglingMarkers(removed).get('gone').length, 1, 'dangling marker surfaced')
// a healthy document reports nothing dangling
assert.strictEqual(m.danglingMarkers(dupDoc).size, 0, 'balanced doc has no dangles')
assert.strictEqual(m.danglingMarkers(doc).size, 0, 'single-thread doc has no dangles')

// --- forward compatibility with other tools ----------------------------
const withUnknown = '<!--\n@thread fw1\n@status open\n@version 2\n@tool someeditor\n\n[user | ts]\nbody\n-->'
const fw = m.parseCommentThreads(withUnknown)
assert.strictEqual(fw.length, 1, 'unknown @ directives no longer fatal')
assert.strictEqual(fw[0].id, 'fw1')
assert.strictEqual(fw[0].messages[0].body, 'body', 'message still recovered')
// unknown directives are ignored, not preserved
assert.ok(!m.serializeCommentThread(fw[0]).includes('@version'), 'unknown directive dropped on re-serialise')
// non-directive junk is still rejected, so malformed blocks stay malformed
assert.strictEqual(m.parseCommentThreads('<!--\n@thread a\n@status open\nhello\n\n[user | ts]\nx\n-->').length, 0,
  'arbitrary prose in metadata still rejected')

// --- FA-17: author is an open vocabulary ------------------------------
const agent = 'architect-agent:75a079f6'
const agentThread = m.createCommentThread(agent, 'Per-agent attribution.')
const agentDoc = m.serializeCommentThread(agentThread)
const agentBack = m.parseCommentThreads(agentDoc)
assert.strictEqual(agentBack.length, 1, 'foreign author no longer drops the thread')
assert.strictEqual(agentBack[0].messages[0].author, agent, 'author round-trips verbatim')
// a colon-bearing agent identity is the motivating case
assert.ok(agent.includes(':'), 'sanity: identity carries a colon')
// replies carry their own author
const twoAuthors = m.appendReply(agentBack[0], 'reviewer-agent:ff01', 'Ack.')
assert.strictEqual(twoAuthors.messages[1].author, 'reviewer-agent:ff01')
const mixed = m.parseCommentThreads(m.serializeCommentThread(twoAuthors))[0]
assert.deepStrictEqual(mixed.messages.map(x => x.author), [agent, 'reviewer-agent:ff01'], 'distinct authors preserved')

// the two reserved characters are refused on write, not silently corrupted
for (const bad of ['has|pipe', 'has]bracket', '']) {
  assert.throws(() => m.createCommentThread(bad, 'x'), /Invalid comment author/, `rejects ${JSON.stringify(bad)}`)
  assert.throws(() => m.appendReply(agentBack[0], bad, 'x'), /Invalid comment author/)
}

// --- FA-18: escaping is symmetric -------------------------------------
for (const body of ['Use the arrow --> here', '-->', 'a -->\nb --> c', 'no arrows at all']) {
  const t = m.createCommentThread('user', body)
  const round = m.parseCommentThreads(m.serializeCommentThread(t))
  assert.strictEqual(round.length, 1, `block survives body ${JSON.stringify(body)}`)
  assert.strictEqual(round[0].messages[0].body, body, `lossless: ${JSON.stringify(body)}`)
}
// the escape is still applied on the wire, so a lone --> cannot close the block
assert.ok(m.serializeCommentThread(m.createCommentThread('user', '-->')).includes('--\\>'),
  'escape still written')
// double round-trip is stable
const tricky = m.createCommentThread('user', 'x --> y')
const once = m.parseCommentThreads(m.serializeCommentThread(tricky))[0]
const twice = m.parseCommentThreads(m.serializeCommentThread(once))[0]
assert.strictEqual(twice.messages[0].body, 'x --> y', 'stable across two round-trips')

// --- glyph -------------------------------------------------------------
assert.ok(m.formatCommentMarker('g1', 'resolved').includes('\u2705'), 'resolved marker uses U+2705')
assert.ok(m.formatCommentMarker('g1').includes('\u{1F4AC}'), 'open marker unchanged')
// both glyphs are still recognised by the scanner
assert.strictEqual(m.parseCommentMarkers(m.formatCommentMarker('g1', 'resolved')).get('g1')[0].status, 'resolved')
assert.strictEqual(m.parseCommentMarkers(m.formatCommentMarker('g1')).get('g1')[0].status, 'open')

// --- regression: a marker must never land inside a thread block --------
{
  const t = m.createCommentThread('user', 'Existing thread body here.')
  const base = `Prose ${m.formatCommentMarker(t.id)} more prose.\n\n${m.serializeCommentThread(t)}\n`
  const insideBody = base.indexOf('Existing thread body')
  assert.notStrictEqual(m.safeMarkerPosition(base, insideBody), insideBody,
    'offset inside a thread block must move')
  assert.strictEqual(m.htmlCommentRanges(base).length, 1, 'thread block seen as an HTML comment')
  // unterminated comment runs to EOF; a comment inside a fence is not a comment
  assert.strictEqual(m.htmlCommentRanges('a\n<!--\nb\n').length, 1)
  assert.strictEqual(m.htmlCommentRanges('```\n<!--\n```\n').length, 0)

  // opening a thread at that offset must not corrupt the existing one
  const opened = m.openThread(base, insideBody, 'user', 'New thread.')
  const both = m.parseCommentThreads(opened.doc)
  assert.strictEqual(both.length, 2, 'two threads')
  const original = both.find(x => x.id === t.id)
  assert.strictEqual(original.messages[0].body, 'Existing thread body here.',
    'original message body untouched')
  assert.strictEqual(original.markers.length, 1)
  assert.strictEqual(opened.thread.markers.length, 1, 'new thread is anchored')
  // and the new marker is in prose, not buried in a comment
  const newMarker = opened.thread.markers[0]
  assert.ok(!m.htmlCommentRanges(opened.doc).some(r => newMarker.from > r.from && newMarker.from < r.to),
    'new marker is not inside any HTML comment')
}

// --- document-level API ------------------------------------------------
{
  let d = 'First para.\n\nSecond para.\n'
  const a = m.openThread(d, d.indexOf('First'), 'agent:a1', 'Question one.')
  d = a.doc
  assert.strictEqual(m.parseCommentThreads(d).length, 1)
  assert.strictEqual(a.thread.markers.length, 1, 'openThread anchors in one step')
  assert.strictEqual(a.thread.messages[0].author, 'agent:a1')

  // append, preserving prior messages and other threads
  const b = m.openThread(d, d.indexOf('Second'), 'agent:b2', 'Question two.')
  d = b.doc
  d = m.appendToThread(d, { id: a.thread.id }, 'agent:c3', 'Answer one.')
  const after = m.parseCommentThreads(d)
  assert.strictEqual(after.length, 2, 'both threads survive an append')
  const threadA = after.find(x => x.id === a.thread.id)
  assert.deepStrictEqual(threadA.messages.map(x => x.body), [ 'Question one.', 'Answer one.' ])
  assert.deepStrictEqual(threadA.messages.map(x => x.author), [ 'agent:a1', 'agent:c3' ])
  assert.strictEqual(after.find(x => x.id === b.thread.id).messages.length, 1, 'other thread untouched')

  // status flips block and marker together
  d = m.setThreadStatus(d, { id: a.thread.id }, 'resolved')
  const resolvedThread = m.parseCommentThreads(d).find(x => x.id === a.thread.id)
  assert.strictEqual(resolvedThread.status, 'resolved', 'block status flipped')
  assert.strictEqual(resolvedThread.markers[0].status, 'resolved', 'marker glyph flipped with it')
  assert.strictEqual(resolvedThread.messages.length, 2, 'resolve preserves every message')
  assert.ok(d.includes('\u2705'), 'resolved glyph present in document')
  // and back again
  d = m.setThreadStatus(d, { id: a.thread.id }, 'open')
  assert.strictEqual(m.parseCommentThreads(d).find(x => x.id === a.thread.id).markers[0].status, 'open')
}

// --- ThreadRef resolution ---------------------------------------------
{
  const DUP2 = 'dupref'
  const blk = (body) => `<!--\n@thread ${DUP2}\n@status open\n\n[user | ts]\n${body}\n-->`
  const d = `x ${m.formatCommentMarker(DUP2)} y ${m.formatCommentMarker(DUP2)} z\n\n${blk('A')}\n\n${blk('B')}\n`

  assert.throws(() => m.resolveThreadRef(d, { id: 'nope' }), /No thread/, 'missing id throws')
  assert.throws(() => m.resolveThreadRef(d, { id: DUP2 }), /Ambiguous/, 'duplicate id without ordinal throws')
  assert.strictEqual(m.resolveThreadRef(d, { id: DUP2, ordinal: 0 }).messages[0].body, 'A')
  assert.strictEqual(m.resolveThreadRef(d, { id: DUP2, ordinal: 1 }).messages[0].body, 'B')
  assert.throws(() => m.resolveThreadRef(d, { id: DUP2, ordinal: 5 }), /ordinal 5/, 'out-of-range ordinal throws')

  // mutating operations refuse an ambiguous ref rather than guessing
  assert.throws(() => m.appendToThread(d, { id: DUP2 }, 'user', 'x'), /Ambiguous/)
  assert.throws(() => m.setThreadStatus(d, { id: DUP2 }, 'resolved'), /Ambiguous/)
  // with an ordinal they act on exactly one
  const only = m.setThreadStatus(d, { id: DUP2, ordinal: 1 }, 'resolved')
  const states = m.parseCommentThreads(only).map(x => x.status)
  assert.deepStrictEqual(states, [ 'open', 'resolved' ], 'only the named block changed')
}

// --- the high-level surface is append-only by construction -------------
assert.strictEqual(typeof m.openThread, 'function')
assert.strictEqual(typeof m.appendToThread, 'function')
assert.strictEqual(typeof m.setThreadStatus, 'function')
for (const absent of [ 'deleteThread', 'removeThread', 'editThreadMessage', 'replaceThread' ]) {
  assert.strictEqual(m[absent], undefined, `${absent} must not exist`)
}

// --- multi-message bodies are stable across repeated round-trips -------
{
  let t = { id: 'stable1', status: 'open', messages: [
    { author: 'a', timestamp: 't1', body: 'first' },
    { author: 'b', timestamp: 't2', body: 'second' },
    { author: 'c', timestamp: 't3', body: 'third' }
  ]}
  const want = [ 'first', 'second', 'third' ]
  for (let i = 0; i < 5; i++) {
    t = m.parseCommentThreads(m.serializeCommentThread(t))[0]
    assert.deepStrictEqual(t.messages.map(x => x.body), want, `bodies stable at trip ${i + 1}`)
  }
  // a body containing a deliberate internal blank line still survives
  let para = { id: 'stable2', status: 'open', messages: [
    { author: 'a', timestamp: 't1', body: 'one\n\ntwo' },
    { author: 'b', timestamp: 't2', body: 'tail' }
  ]}
  para = m.parseCommentThreads(m.serializeCommentThread(para))[0]
  assert.strictEqual(para.messages[0].body, 'one\n\ntwo', 'internal blank line preserved')
}

console.log('ALL PASS —', 'protocol + document API: 12 groups')
