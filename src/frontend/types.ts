export type Chapter = { number?: number; title?: string; start: number; end?: number | null }
export type Track = {
  id: string
  book_id: string
  title: string
  name: string
  path?: string
  duration: number
  chapters: Chapter[]
  chapter_count?: number
  format?: string
}
export type Progress = { track_id: string; position: number; updated_at?: string; revision?: number; completed?: boolean; book_position?: number; fraction?: number | null; status?: 'not-started' | 'in-progress' | 'completed' }
export type MetadataCandidate = { source: 'Google Books' | 'Open Library'; source_id: string; title: string; authors: string[]; description?: string; publisher?: string; published_date?: string; isbn?: string; cover?: string }
export type Book = {
  id: string
  title: string
  artist?: string | null
  album_artist?: string | null
  album?: string | null
  directory?: string | null
  cover?: string | null
  duration: number
  added_at?: string
  track_count?: number
  favorite?: boolean
  rating?: number | null
  tags?: string[]
  tracks: Track[]
  progress?: Progress | null
}
