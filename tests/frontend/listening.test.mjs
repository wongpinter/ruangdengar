import assert from 'node:assert/strict'
import test from 'node:test'
import { resumeTrack, listeningStatus, newerCheckpoint, persistCheckpoint, editionQueue, uniqueBooks, registerProgress } from '../../src/frontend/listening.ts'

const book = { id: 'book', tracks: [{ id: 'first', duration: 100 }, { id: 'last', duration: 100 }], progress: { track_id: 'last', position: 30 } }

test('resume selects saved part and falls back when it was removed', () => {
  assert.equal(resumeTrack(book).id, 'last')
  assert.equal(resumeTrack({ ...book, progress: { track_id: 'removed', position: 30 } }).id, 'first')
})

test('completion requires the final part; zero seconds on later parts is in progress', () => {
  assert.equal(listeningStatus({ ...book, progress: { track_id: 'first', position: 99 } }), 'in-progress')
  assert.equal(listeningStatus({ ...book, progress: { track_id: 'last', position: 0 } }), 'in-progress')
  assert.equal(listeningStatus({ ...book, progress: { track_id: 'last', position: 99 } }), 'completed')
  assert.equal(listeningStatus({ ...book, progress: null }), 'not-started')
})

test('server progress takes precedence over older or undated local checkpoints', () => {
  const serverBook = { ...book, progress: { ...book.progress, updated_at: '2026-09-30T10:00:00.000Z' } }
  const checkpoint = { bookId: 'book', trackId: 'last', position: 80, savedAt: Date.parse('2026-09-30T09:00:00Z') }
  assert.equal(newerCheckpoint(serverBook, checkpoint), null)
  assert.equal(newerCheckpoint(serverBook, { ...checkpoint, savedAt: undefined }), null)
  const newer = { ...checkpoint, savedAt: Date.parse('2026-09-30T11:00:00Z') }
  assert.equal(newerCheckpoint(serverBook, newer), newer)
  assert.equal(newerCheckpoint({ ...serverBook, progress: { ...book.progress, updated_at: '2026-09-30 12:00:00' } }, newer), null)
  assert.equal(newerCheckpoint(serverBook, { ...newer, trackId: 'removed' }), null)
})


test('page-close progress uses a keepalive PUT matching the API', async () => {
  const original = globalThis.fetch
  let captured
  globalThis.fetch = async (url, options) => { captured = { url, options }; return Response.json({ revision: 1 }) }
  try {
    const savedAt = Date.now()
    await persistCheckpoint({ bookId: 'book/a', trackId: 'last', position: 32, savedAt, baseRevision: 0, eventId: "checkpoint-1" }, true)
    assert.equal(captured.url, '/api/v1/books/book%2Fa/progress')
    assert.equal(captured.options.method, 'PUT')
    assert.equal(captured.options.keepalive, true)
    assert.deepEqual(JSON.parse(captured.options.body), { track_id: 'last', position: 32, base_revision: 0, event_id: 'checkpoint-1' })
  } finally { globalThis.fetch = original }
})


test('same title and artist editions stay distinct in queues and shelves', () => {
  const first = { ...book, id: 'edition-one', album: 'Same', artist: 'Author' }
  const second = { ...book, id: 'edition-two', album: 'Same', artist: 'Author', tracks: [{ id: 'other', duration: 30 }] }
  assert.deepEqual(editionQueue(first).map(item => item.track.id), ['first', 'last'])
  assert.deepEqual(uniqueBooks([first, second, first]).map(item => item.id), ['edition-one', 'edition-two'])
})

test('revision comparisons ignore device clock skew and server completion is authoritative', () => {
  const current = { ...book, progress: { ...book.progress, revision: 3, status: 'completed' } }
  const saved = { bookId: 'book', trackId: 'last', position: 10, savedAt: 1, baseRevision: 3 }
  assert.equal(newerCheckpoint(current, saved), saved)
  assert.equal(newerCheckpoint(current, { ...saved, baseRevision: 2, savedAt: Date.now() + 99999 }), null)
  assert.equal(listeningStatus(current), 'completed')
})

test('queued saves use their own acknowledged predecessor, not newly fetched remote revisions', async () => {
  const original = globalThis.fetch
  const bodies = []
  let finish
  globalThis.fetch = async (_, options) => {
    bodies.push(JSON.parse(options.body))
    if (bodies.length === 1) return new Promise(resolve => { finish = resolve })
    return Response.json({ revision: 2 })
  }
  try {
    const checkpoint = { bookId: 'queued', trackId: 'first', position: 10, savedAt: 1, baseRevision: 0, eventId: 'a' }
    const first = persistCheckpoint(checkpoint)
    const second = persistCheckpoint({ ...checkpoint, position: 20, eventId: 'b' })
    registerProgress('queued', { revision: 100 })
    finish(Response.json({ revision: 1 }))
    await Promise.all([first, second])
    assert.deepEqual(bodies.map(body => body.base_revision), [0, 1])
  } finally { globalThis.fetch = original }
})

test('stale offline writes are never silently rebased onto a remote checkpoint', async () => {
  const original = globalThis.fetch
  let body
  globalThis.fetch = async (_, options) => { body = JSON.parse(options.body); return Response.json({}, { status: 409 }) }
  try {
    registerProgress('offline', { revision: 10 })
    await persistCheckpoint({ bookId: 'offline', trackId: 'first', position: 5, savedAt: 1, baseRevision: 2, eventId: 'offline-event' })
    assert.equal(body.base_revision, 2)
  } finally { globalThis.fetch = original }
})
