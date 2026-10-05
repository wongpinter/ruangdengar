import type { Book, Progress, Track } from './types'

export type Checkpoint = { bookId: string; trackId: string; position: number; savedAt: number; baseRevision?: number; eventId?: string; completed?: boolean }

export function resumeTrack(book: Book): Track | undefined {
  return book.tracks.find(track => track.id === book.progress?.track_id) ?? book.tracks[0]
}

export function listeningStatus(book: Book): 'in-progress' | 'not-started' | 'completed' {
  if (book.progress?.status) return book.progress.status
  const index = book.tracks.findIndex(track => track.id === book.progress?.track_id)
  if (index < 0) return 'not-started'
  const position = book.progress?.position ?? 0
  const track = book.tracks[index]
  if (index === book.tracks.length - 1 && track.duration > 0 && position / track.duration >= 0.95) return 'completed'
  return index > 0 || position > 0 ? 'in-progress' : 'not-started'
}

export function newerCheckpoint(
  book: { id: string; tracks: { id: string }[]; progress?: Progress | null },
  checkpoint: Checkpoint | null,
): Checkpoint | null {
  if (!checkpoint || checkpoint.bookId !== book.id || !book.tracks.some(track => track.id === checkpoint.trackId)) return null
  if (!Number.isFinite(checkpoint.savedAt) || !Number.isFinite(checkpoint.position) || checkpoint.position < 0) return null
  if (checkpoint.baseRevision !== undefined) {
    return checkpoint.baseRevision === (book.progress?.revision ?? 0) ? checkpoint : null
  }
  // Unversioned checkpoints from an old client cannot safely overwrite revised state.
  if (book.progress?.revision !== undefined) return null
  // Older SQLite timestamps are UTC even though they omit the timezone.
  const timestamp = book.progress?.updated_at
  const serverTime = timestamp ? Date.parse(timestamp.endsWith('Z') ? timestamp : `${timestamp.replace(' ', 'T')}Z`) : 0
  return Number.isFinite(serverTime) && checkpoint.savedAt > serverTime ? checkpoint : null
}

export function persistCheckpoint(checkpoint: Checkpoint, keepalive = false): Promise<Response> {
  const send = async (revision = checkpoint.baseRevision ?? 0) => {
    const response = await fetch(`/api/v1/books/${encodeURIComponent(checkpoint.bookId)}/progress`, {
      method: 'PUT', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ track_id: checkpoint.trackId, position: checkpoint.position,
        base_revision: revision,
        event_id: checkpoint.eventId ?? `${checkpoint.trackId}:${checkpoint.savedAt}`,
        ...(checkpoint.completed === undefined ? {} : { completed: checkpoint.completed }) }),
      keepalive,
    })
    if (response.ok) {
      const accepted = await response.clone().json() as Progress
      registerProgress(checkpoint.bookId, accepted)
    }
    return response
  }
  const previous = pendingWrites.get(checkpoint.bookId)
  const result = previous ? previous.then(async response => {
    if (!response.ok) return response.clone()
    const accepted = await response.clone().json() as Progress
    return send(Math.max(checkpoint.baseRevision ?? 0, accepted.revision ?? 0))
  }, () => send()) : send()
  pendingWrites.set(checkpoint.bookId, result)
  void result.finally(() => { if (pendingWrites.get(checkpoint.bookId) === result) pendingWrites.delete(checkpoint.bookId) }).catch(() => {})
  return result
}

// Acknowledged revisions are separate from optimistic UI positions.
const revisions = new Map<string, number>()
const pendingWrites = new Map<string, Promise<Response>>()

export function registerProgress(bookId: string, progress?: Progress | null): void {
  revisions.set(bookId, pendingWrites.has(bookId) ? Math.max(revisions.get(bookId) ?? 0, progress?.revision ?? 0) : progress?.revision ?? 0)
}

export function progressRevision(bookId: string): number {
  return revisions.get(bookId) ?? 0
}

export function editionQueue(book: Book): { book: Book; track: Track }[] {
  return book.tracks.map(track => ({ book, track }))
}

export function uniqueBooks(books: Book[]): Book[] {
  return [...new Map(books.map(book => [book.id, book])).values()]
}
