import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from 'react'
import { Link, Route, Routes, useLocation, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { ArrowLeft, BookOpen, CheckCircle2, Clock3, Headphones, Library, LoaderCircle, Search, Settings2, SlidersHorizontal, Heart, Star, Plus } from 'lucide-react'
import { Avatar, Button, Card, Cover, Input, PlayIcon, Progress, Skeleton } from './components/ui'
import { PlayerBar } from './components/PlayerBar'
import type { Book, Chapter, MetadataCandidate, Progress as ListeningProgress, Track } from './types'
import { resumeTrack, listeningStatus, newerCheckpoint, persistCheckpoint, registerProgress, progressRevision, editionQueue, uniqueBooks, type Checkpoint } from './listening'

type ScanStatus = { status: string; total: number; processed: number; current: string; error: string }
type Features = { favorites: string[]; ratings: Record<string, number>; tags: Record<string, string[]>; playlists: { id: string; name: string; book_ids: string[]; revision: number }[]; history: { book_id: string; track_id: string; position: number; played_at: string }[] }
type StorageInfo = { tracks: number; bytes: number; source_bytes: number; cached_bytes: number; cache_budget_bytes: number; formats: { mime_type: string; count: number }[] }

async function api<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetch(url, { credentials: 'same-origin', ...options })
  if (!response.ok) {
    const body = await response.json().catch(() => null) as { detail?: string | { code?: string } } | null
    throw new Error(response.status === 401 ? 'Sign in to open your library.' : typeof body?.detail === 'string' ? body.detail : `Request failed (${response.status}).`)
  }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

function App() {
  const [books, setBooks] = useState<Book[]>([])
  const [filter, setFilter] = useState('')
  const [statusFilter, setStatusFilter] = useState<'all' | 'in-progress' | 'not-started' | 'completed'>('all')
  const [sortBy, setSortBy] = useState<'title' | 'author' | 'duration' | 'rating' | 'added'>('title')
  const [sortDirection, setSortDirection] = useState<'asc' | 'desc'>('desc')
  const [groupMode, setGroupMode] = useState<'artist' | 'directory' | 'album' | 'album-artist'>('album-artist')
  const [favoritesOnly, setFavoritesOnly] = useState(false)
  const [playlistView, setPlaylistView] = useState<string | null>(null)
  const [error, setError] = useState('')
  const [playbackError, setPlaybackError] = useState('')
  const [loading, setLoading] = useState(true)
  const [scan, setScan] = useState<ScanStatus | null>(null)
  const [features, setFeatures] = useState<Features>({ favorites: [], ratings: {}, tags: {}, playlists: [], history: [] })
  const [storage, setStorage] = useState<StorageInfo | null>(null)
  const [featureError, setFeatureError] = useState('')
  const [activeBook, setActiveBook] = useState<Book | null>(null)
  const [activeTrack, setActiveTrack] = useState<Track | null>(null)
  const [startAt, setStartAt] = useState(0)
  const [autoPlay, setAutoPlay] = useState(false)
  const [resumeBook, setResumeBook] = useState<Book | null>(null)
  const navigate = useNavigate()
  const progressState = useRef({ lastSentAt: 0, conflicted: false, latest: null as Checkpoint | null })
  const prefetchedTracks = useRef(new Set<string>())
  const currentListen = useRef<{ bookId: string; trackId: string } | null>(null)

  const load = useCallback(async () => {
    try {
      const data = await api<{ books: Book[]; features: Features; storage: StorageInfo; scan: ScanStatus }>('/api/bootstrap')
      const result = data.books
      const recent = data.features
      result.forEach(book => registerProgress(book.id, book.progress))
      setBooks(result); setFeatures(recent); setStorage(data.storage); setScan(data.scan)
      const lastPlayed = recent.history[0]?.book_id
      let saved: Checkpoint | null = null
      try { saved = JSON.parse(localStorage.getItem('ruangdengar.last-listening') || 'null') } catch { /* storage can be disabled */ }
      const savedBook = saved && result.find(item => item.id === saved?.bookId && item.tracks.some(track => track.id === saved?.trackId))
      let pending: Checkpoint | null = null
      try { pending = JSON.parse(localStorage.getItem('ruangdengar.pending-progress') || 'null') } catch { /* storage can be disabled */ }
      const latestBook = result.find(item => item.id === lastPlayed)
      const book = latestBook || savedBook
      const candidates = book ? [newerCheckpoint(book, pending), newerCheckpoint(book, saved)].filter((item): item is Checkpoint => !!item) : []
      const local = candidates.sort((a, b) => b.savedAt - a.savedAt)[0]
      const recovery = local && book
        ? { ...book, progress: { ...book.progress, track_id: local.trackId, position: local.position, revision: local.baseRevision } }
        : book?.progress?.track_id ? book : null
      setResumeBook(recovery || null)
      result.filter(book => (book.progress?.position ?? 0) > 0).slice(0, 3).forEach(book => { const track = book.tracks.find(item => item.id === book.progress?.track_id); if (track) void fetch(`/api/tracks/${encodeURIComponent(track.id)}/warm`, { method: 'POST', credentials: 'same-origin' }).catch(() => {}) })
      setError('')
    } catch (e) { setError(e instanceof Error ? e.message : 'Could not load library.') }
    finally { setLoading(false) }
  }, [])
  const loadFeatures = useCallback(async () => {
    try { const [data, info] = await Promise.all([api<Features>('/api/features'), api<StorageInfo>('/api/storage')]); setFeatures(data); setStorage(info); setFeatureError('') }
    catch (e) { setFeatureError(e instanceof Error ? e.message : 'Could not load library tools.') }
  }, [])
  useEffect(() => { void load() }, [load])
  const refreshLibrary = useCallback(async () => {
    try {
      const response = await fetch('/api/library/refresh', { method: 'POST', credentials: 'same-origin' })
      if (!response.ok && response.status !== 409) throw new Error(await response.text())
      setScan(previous => ({ status: 'running', total: previous?.total ?? 0, processed: previous?.processed ?? 0, current: 'Connecting to Google Drive', error: '' }))
    } catch (e) { setError(e instanceof Error ? e.message : 'Could not start library scan.') }
  }, [])
  const scanRunning = scan?.status === 'running'
  useEffect(() => {
    if (!scanRunning) return
    const events = new EventSource('/api/library/events')
    events.onmessage = event => {
      const next = JSON.parse(event.data) as ScanStatus
      setScan(next)
      if (next.status === 'completed') { events.close(); void load() }
      else if (next.status === 'failed') events.close()
    }
    return () => events.close()
  }, [load, scanRunning])
  const filtered = useMemo(() => books
    .filter(book => !favoritesOnly || features.favorites.includes(book.id))
    .filter(book => !playlistView || features.playlists.find(item => item.id === playlistView)?.book_ids.includes(book.id))
    .filter(book => `${book.title} ${book.artist ?? ''} ${(features.tags[book.id] ?? []).join(' ')}`.toLowerCase().includes(filter.trim().toLowerCase()))
    .filter(book => {
      if (statusFilter !== 'all') return listeningStatus(book) === statusFilter
      return true
    })
    .sort((a, b) => {
      const order = sortBy === 'author' ? (a.artist ?? '').localeCompare(b.artist ?? '') || a.title.localeCompare(b.title)
        : sortBy === 'duration' ? a.duration - b.duration
          : sortBy === 'rating' ? (features.ratings[a.id] ?? 0) - (features.ratings[b.id] ?? 0)
            : sortBy === 'added' ? (a.added_at ?? '').localeCompare(b.added_at ?? '')
              : a.title.localeCompare(b.title)
      return sortDirection === 'asc' ? order || a.id.localeCompare(b.id) : -(order || a.id.localeCompare(b.id))
    }),
  [books, filter, statusFilter, sortBy, sortDirection, features, favoritesOnly, playlistView])
  const play = (book: Book, track: Track, start = 0) => {
    registerProgress(book.id, book.progress)
    progressState.current.conflicted = false
    setPlaybackError('')
    currentListen.current = { bookId: book.id, trackId: track.id }
    try { localStorage.setItem('ruangdengar.last-listening', JSON.stringify({ bookId: book.id, trackId: track.id, position: start, savedAt: Date.now(), baseRevision: progressRevision(book.id), eventId: crypto.randomUUID() })) } catch { /* storage can be disabled */ }
    setResumeBook(null)
    setAutoPlay(true)
    void api(`/api/history/${encodeURIComponent(book.id)}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ track_id: track.id, position: start }) }).then(loadFeatures).catch(() => setFeatureError('Could not update listening history.'))
    void fetch(`/api/tracks/${encodeURIComponent(track.id)}/warm`, { method: 'POST', credentials: 'same-origin' }).catch(() => {})
    setActiveBook(book); setActiveTrack(track); setStartAt(start)
  }
  const trackQueue = useMemo(() => {
    if (!activeBook || !activeTrack) return []
    return editionQueue(activeBook)
  }, [activeBook, activeTrack])
  const queueIndex = trackQueue.findIndex(item => item.track.id === activeTrack?.id)
  const moveTrack = (delta: number) => {
    const index = queueIndex
    if (index < 0 || !trackQueue.length) return
    const next = trackQueue[index + delta]
    if (next) play(next.book, next.track)
  }
  const updateBookFeature = async (bookId: string, kind: 'favorite' | 'rating' | 'tags', value: boolean | number | string[]) => {
    const result = await api<Record<string, unknown>>(`/api/books/${encodeURIComponent(bookId)}/${kind}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ [kind]: kind === 'rating' && value === 0 ? null : value }) })
    if (kind === 'favorite') setFeatures(previous => ({ ...previous, favorites: value ? [...new Set([...previous.favorites, bookId])] : previous.favorites.filter(id => id !== bookId) }))
    if (kind === 'rating') setFeatures(previous => { const ratings = { ...previous.ratings }; if (result.rating) ratings[bookId] = result.rating as number; else delete ratings[bookId]; return { ...previous, ratings } })
    if (kind === 'tags') setFeatures(previous => ({ ...previous, tags: { ...previous.tags, [bookId]: result.tags as string[] } }))
  }
  const makePlaylist = async () => {
    const name = window.prompt('Playlist name')?.trim()
    if (!name) return
    try { const playlist = await api<Features['playlists'][number]>('/api/playlists', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }) }); setFeatures(previous => ({ ...previous, playlists: [...previous.playlists, playlist] })) }
    catch (e) { setFeatureError(e instanceof Error ? e.message : 'Could not create playlist.') }
  }
  const addToPlaylist = async (playlistId: string, bookId: string) => {
    const playlist = features.playlists.find(item => item.id === playlistId)
    if (!playlist || playlist.book_ids.includes(bookId)) return
    try { const result = await api<Features['playlists'][number]>(`/api/playlists/${encodeURIComponent(playlistId)}/books/${encodeURIComponent(bookId)}`, { method: 'PUT' }); setFeatures(previous => ({ ...previous, playlists: previous.playlists.map(item => item.id === playlistId ? result : item) })) }
    catch (e) { setFeatureError(e instanceof Error ? e.message : 'Could not update playlist.') }
  }
  const saveProgress = useCallback((time: number, force = false) => {
    if (activeBook && activeTrack?.duration && time / activeTrack.duration >= 0.8) {
      const index = activeBook.tracks.findIndex(track => track.id === activeTrack.id)
      const next = activeBook.tracks[index + 1]
      if (next && !prefetchedTracks.current.has(next.id)) {
        prefetchedTracks.current.add(next.id)
        void fetch(`/api/tracks/${encodeURIComponent(next.id)}/warm`, { method: 'POST', credentials: 'same-origin' }).catch(() => {})
      }
    }
    if (progressState.current.conflicted || !activeBook || !activeTrack || !Number.isFinite(time) || time < 0) return
    const checkpoint: Checkpoint = { bookId: activeBook.id, trackId: activeTrack.id, position: Math.floor(time), savedAt: Date.now(), baseRevision: progressRevision(activeBook.id), eventId: crypto.randomUUID(), completed: activeBook.tracks.at(-1)?.id === activeTrack.id && activeTrack.duration > 0 && time >= activeTrack.duration - 0.5 }
    if (currentListen.current?.bookId === checkpoint.bookId && currentListen.current.trackId === checkpoint.trackId) {
      try { localStorage.setItem('ruangdengar.last-listening', JSON.stringify(checkpoint)) } catch { /* storage can be disabled */ }
    }
    progressState.current.latest = checkpoint
    try { localStorage.setItem('ruangdengar.pending-progress', JSON.stringify(checkpoint)) } catch { /* storage can be disabled */ }
    if (!force && Date.now() - progressState.current.lastSentAt < 15_000) return
    progressState.current.lastSentAt = Date.now()
    setBooks(previous => previous.map(book => book.id === checkpoint.bookId ? { ...book, progress: { ...book.progress, track_id: checkpoint.trackId, position: checkpoint.position, updated_at: new Date(checkpoint.savedAt).toISOString() } } : book))
    void persistCheckpoint(checkpoint).then(async response => {
      if (response.status === 409) {
        setPlaybackError('Your listening position changed in another session. Resume from the saved position to continue.')
        if (currentListen.current?.bookId === checkpoint.bookId) {
          progressState.current.conflicted = true
          progressState.current.latest = null
          document.querySelector<HTMLAudioElement>('.player-bar audio')?.pause()
          setActiveBook(null); setActiveTrack(null)
          try {
            const stored = JSON.parse(localStorage.getItem('ruangdengar.pending-progress') || 'null') as Checkpoint | null
            if (stored?.bookId === checkpoint.bookId) localStorage.removeItem('ruangdengar.pending-progress')
          } catch { /* storage can be disabled */ }
        }
        void load()
        return
      }
      if (!response.ok) throw new Error(`Progress save failed: ${response.status}`)
      const accepted = await response.json() as ListeningProgress
      const latest = progressState.current.latest
      if (latest && latest !== checkpoint && latest.bookId === checkpoint.bookId && (latest.baseRevision ?? 0) < (accepted.revision ?? 0)) {
        latest.baseRevision = accepted.revision
        try {
          const stored = JSON.parse(localStorage.getItem('ruangdengar.pending-progress') || 'null') as Checkpoint | null
          if (stored?.eventId === latest.eventId) localStorage.setItem('ruangdengar.pending-progress', JSON.stringify(latest))
        } catch { /* storage can be disabled */ }
      }
      setBooks(previous => previous.map(book => book.id === checkpoint.bookId ? { ...book, progress: accepted } : book))
      if (progressState.current.latest === checkpoint) { try { localStorage.removeItem('ruangdengar.pending-progress') } catch { /* storage can be disabled */ } }
    }).catch(() => setPlaybackError('Your position is saved on this device. It will retry when you reconnect.'))
  }, [activeBook, activeTrack, load])
  useEffect(() => {
    const flush = () => {
      const checkpoint = progressState.current.latest
      if (!checkpoint) return
      void persistCheckpoint(checkpoint, true).catch(() => {})
    }
    const retry = () => {
      let stored: string | null
      let pending: Checkpoint | null = null
      try { stored = localStorage.getItem('ruangdengar.pending-progress'); pending = JSON.parse(stored || 'null') } catch { return }
      if (!pending) return
      const checkpoint = pending
      void api<ListeningProgress | null>(`/api/progress/${encodeURIComponent(checkpoint.bookId)}`).then(async progress => {
        if (localStorage.getItem('ruangdengar.pending-progress') !== stored) return
        if (newerCheckpoint({ id: checkpoint.bookId, tracks: [{ id: checkpoint.trackId }], progress }, checkpoint)) {
          const response = await persistCheckpoint(checkpoint)
          if (!response.ok) return
        }
        if (localStorage.getItem('ruangdengar.pending-progress') === stored) localStorage.removeItem('ruangdengar.pending-progress')
      }).catch(() => {})
    }
    window.addEventListener('pagehide', flush)
    window.addEventListener('online', retry)
    document.addEventListener('visibilitychange', flush)
    retry()
    return () => { window.removeEventListener('pagehide', flush); window.removeEventListener('online', retry); document.removeEventListener('visibilitychange', flush) }
  }, [])

  const openBook = (book: Book) => navigate(`/book/${encodeURIComponent(book.id)}`)
  const playBook = (book: Book) => { const track = resumeTrack(book); if (track) play(book, track, book.progress?.track_id === track.id ? book.progress.position : 0) }
  const groupLink = (mode: 'artist' | 'directory' | 'album' | 'album-artist', name: string) => navigate(`/group/${mode}?name=${encodeURIComponent(name)}`)
  return <div className="app-shell">
    <header className="app-header">
      <Link className="brand" to="/"><span className="brand-mark"><Headphones size={19} /></span><span>Ruang<span className="brand-accent">Dengar</span></span></Link>
      <nav className="desktop-nav" aria-label="Main navigation"><Link to="/">Home</Link><Link to="/search">Search</Link><Link to="/library">Library</Link></nav>
      <div className="header-actions"><Button className="scan-action" disabled={scan?.status === 'running'} onClick={() => void refreshLibrary()}>{scan?.status === 'running' ? 'Scanning…' : 'Scan Drive'}</Button><Link className="settings-link" to="/settings" aria-label="Settings"><Settings2 size={19} /></Link></div>
    </header>
    <main className="main-content">{playbackError && <div className="empty-state" role="alert">{playbackError}</div>}<Routes>
      <Route path="/" element={<HomePage books={filtered} features={features} scan={scan} error={error} onOpen={openBook} onPlay={playBook} onLibrary={() => navigate('/library')} onRefresh={() => void refreshLibrary()} />} />
      <Route path="/search" element={<SearchPage query={filter} setQuery={setFilter} onOpen={openBook} />} />
      <Route path="/library" element={<LibraryPage sortDirection={sortDirection} setSortDirection={setSortDirection} groupMode={groupMode} setGroupMode={setGroupMode} onOpenGroup={groupLink} books={filtered} allBooks={books} features={features} storage={storage} featureError={featureError} onFeature={updateBookFeature} favoritesOnly={favoritesOnly} setFavoritesOnly={setFavoritesOnly} onCreatePlaylist={makePlaylist} onAddToPlaylist={addToPlaylist} onPlaylistView={setPlaylistView} playlistView={playlistView} reloadFeatures={loadFeatures} query={filter} setQuery={setFilter} statusFilter={statusFilter} setStatusFilter={setStatusFilter} sortBy={sortBy} setSortBy={setSortBy} error={error} loading={loading} scan={scan} onRefresh={() => void refreshLibrary()} onOpen={openBook} onPlay={playBook} />} />
      <Route path="/group/:mode" element={<GroupPage books={filtered} onOpen={openBook} onOpenGroup={groupLink} />} />
      <Route path="/book/:bookId" element={<BookPage onPlay={play} />} />
      <Route path="/settings" element={<SettingsPage reloadLibrary={load} />} />
      <Route path="*" element={<div className="empty-state">Page not found.</div>} />
    </Routes></main>
    <BottomNavigation />
    <PlayerBar book={activeBook ?? resumeBook} track={activeTrack ?? (resumeBook?.tracks.find(item => item.id === resumeBook.progress?.track_id) ?? null)} start={activeTrack ? startAt : resumeBook?.progress?.position ?? 0} autoPlay={autoPlay} onResume={() => { if (!activeTrack && resumeBook) { const track = resumeBook.tracks.find(item => item.id === resumeBook.progress?.track_id); if (track) play(resumeBook, track, resumeBook.progress?.position ?? 0) } else { void document.querySelector<HTMLAudioElement>('.player-bar audio')?.play() } }} canNext={activeTrack ? queueIndex >= 0 && queueIndex < trackQueue.length - 1 : false} canPrevious={queueIndex > 0} onNext={() => moveTrack(1)} onPrevious={() => moveTrack(-1)} onTime={saveProgress} onCheckpoint={time => saveProgress(time, true)} />
  </div>
}

function BottomNavigation() {
  const { pathname } = useLocation()
  return <nav className="bottom-navigation" aria-label="Main navigation">
    <Link to="/" aria-current={pathname === '/' ? 'page' : undefined}><Headphones size={19} /><span>Home</span></Link>
    <Link to="/search" aria-current={pathname === '/search' ? 'page' : undefined}><Search size={19} /><span>Search</span></Link>
    <Link to="/library" aria-current={pathname.startsWith('/library') || pathname.startsWith('/group') ? 'page' : undefined}><Library size={19} /><span>Library</span></Link>
    <Link to="/settings" aria-current={pathname === '/settings' ? 'page' : undefined}><Settings2 size={19} /><span>You</span></Link>
  </nav>
}

function HomePage({ books, features, scan, error, onOpen, onPlay, onLibrary, onRefresh }: { books: Book[]; features: Features; scan: ScanStatus | null; error: string; onOpen: (book: Book) => void; onPlay: (book: Book) => void; onLibrary: () => void; onRefresh: () => void }) {
  const booksById = new Map(books.map(book => [book.id, book]))
  const listening = uniqueBooks([...features.history.map(item => booksById.get(item.book_id)).filter((book): book is Book => !!book), ...books.filter(book => (book.progress?.book_position ?? book.progress?.position ?? 0) > 0)]).slice(0, 5)
  return <section className="home-page">
    <div className="home-greeting"><div><span className="eyebrow">YOUR PERSONAL LIBRARY</span><h1>Good stories,<br />good company.</h1><p>Your next chapter is waiting.</p></div><Avatar className="home-avatar">A</Avatar></div>
    {scan && <ScanProgress scan={scan} onRefresh={onRefresh} />}
    {error && <div className="empty-state">{error} <a href="/auth/google">Sign in</a></div>}
    <SectionHeader title="Continue listening" action="View library" onAction={onLibrary} />
    {listening.length ? <div className="home-book-shelf">{listening.map(book => <button className="home-book-tile" key={book.id} onClick={() => onPlay(book)}><Cover className="home-book-cover" src={book.cover || '/icon.svg'} alt={`${book.title} cover`} /><span className="home-book-copy"><strong>{book.title}</strong><small>{book.artist || 'Audiobook'}</small><Progress value={(book.progress?.fraction ?? 0) * 100} label={`${book.title} listening progress`} /></span></button>)}</div> : <div className="home-empty"><Headphones size={24} /><p>Your listening shelf will appear here.</p><Button onClick={onLibrary}>Browse library</Button></div>}
    <SectionHeader title="Your library" subtitle={`${books.length} audiobooks`} action="Browse all" onAction={onLibrary} />
    {books.length ? <div className="home-book-grid">{books.slice(0, 8).map(book => <button className="home-library-tile" key={book.id} onClick={() => onOpen(book)}><Cover className="home-library-cover" src={book.cover || '/icon.svg'} alt={`${book.title} cover`} /><strong>{book.title}</strong><small>{book.artist || 'Audiobook'}</small></button>)}</div> : <div className="home-empty"><p>Your Drive library is ready when you are.</p><Button onClick={onLibrary}>Open library</Button></div>}
    {features.playlists.length > 0 && <><SectionHeader title="Playlists" action="Manage" onAction={onLibrary} /><div className="home-playlists">{features.playlists.slice(0, 4).map((playlist, index) => <button key={playlist.id} onClick={onLibrary}><span className={`playlist-art playlist-art-${index % 4}`}><Headphones size={22} /></span><span><strong>{playlist.name}</strong><small>{playlist.book_ids.length} audiobooks</small></span></button>)}</div></>}
  </section>
}

function SearchPage({ query, setQuery, onOpen }: { query: string; setQuery: (value: string) => void; onOpen: (book: Book) => void }) {
  const [books, setBooks] = useState<Book[]>([])
  const [cursor, setCursor] = useState<string | null>(null)
  const [total, setTotal] = useState(0)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const requestId = useRef(0)
  const readPage = useCallback(async (next?: string) => {
    const id = ++requestId.current
    setLoading(true)
    try {
      const page = await api<{ items: Omit<Book, 'tracks'>[]; next_cursor: string | null; total: number }>(`/api/v1/books?q=${encodeURIComponent(query)}&limit=50${next ? `&cursor=${encodeURIComponent(next)}` : ''}`)
      if (id !== requestId.current) return
      const items = page.items.map(book => ({ ...book, tracks: [] }))
      setBooks(previous => next ? uniqueBooks([...previous, ...items]) : items)
      setCursor(page.next_cursor); setTotal(page.total); setError('')
    } catch (e) { if (id === requestId.current) setError(e instanceof Error ? e.message : 'Could not search.') }
    finally { if (id === requestId.current) setLoading(false) }
  }, [query])
  useEffect(() => {
    setBooks([]); setCursor(null); setTotal(0); setError('')
    const timer = window.setTimeout(() => { if (query.trim()) void readPage() }, 250)
    return () => { window.clearTimeout(timer); requestId.current++ }
  }, [query, readPage])
  return <section className="search-page"><span className="eyebrow">FIND YOUR NEXT LISTEN</span><h1>Search</h1><label className="search-box"><Search size={19} /><Input autoFocus placeholder="Search audiobooks, authors, tags…" value={query} onChange={event => setQuery(event.target.value)} /><button type="button" aria-label="Clear search" onClick={() => setQuery('')} disabled={!query}>×</button></label><p className="search-results-count">{query ? `${total} matching audiobooks` : 'Search your collection by title, author, or tag.'}</p>{query && <div className="media-grid">{books.map((book, index) => <MediaCard key={book.id} book={book} index={index} favorite={false} rating={0} tags={[]} playlists={[]} onFavorite={() => {}} onRate={() => {}} onTag={() => {}} onAddToPlaylist={() => {}} onOpen={() => onOpen(book)} onPlay={() => onOpen(book)} />)}</div>}{error && <div className="empty-state">{error}<Button onClick={() => void readPage()}>Retry search</Button></div>}{cursor && <Button disabled={loading} onClick={() => void readPage(cursor)}>Load more</Button>}{loading && <p role="status">Searching…</p>}{query && !loading && !error && !books.length && <div className="empty-state">No audiobooks match “{query}”.</div>}</section>
}

function SectionHeader({ title, subtitle, action, onAction }: { title: string; subtitle?: string; action?: string; onAction?: () => void }) {
  return <div className="section-heading"><div><h2>{title}</h2>{subtitle && <p>{subtitle}</p>}</div>{action && <button className="section-action" onClick={onAction}>{action}<ArrowLeft className="section-action-arrow" size={15} /></button>}</div>
}

function LibraryPage({ groupMode, setGroupMode, onOpenGroup, books, allBooks, features, storage, featureError, onFeature, favoritesOnly, setFavoritesOnly, playlistView, onPlaylistView, reloadFeatures, onCreatePlaylist, onAddToPlaylist, query, setQuery, statusFilter, setStatusFilter, sortBy, setSortBy, sortDirection, setSortDirection, error, loading, scan, onRefresh, onOpen, onPlay }: { groupMode: 'artist' | 'directory' | 'album' | 'album-artist'; setGroupMode: (value: 'artist' | 'directory' | 'album' | 'album-artist') => void; onOpenGroup: (mode: 'artist' | 'directory' | 'album' | 'album-artist', name: string) => void; books: Book[]; allBooks: Book[]; features: Features; storage: StorageInfo | null; featureError: string; onFeature: (bookId: string, kind: 'favorite' | 'rating' | 'tags', value: boolean | number | string[]) => void; favoritesOnly: boolean; setFavoritesOnly: (value: boolean) => void; playlistView: string | null; onPlaylistView: (value: string | null) => void; reloadFeatures: () => void; onCreatePlaylist: () => void; onAddToPlaylist: (playlistId: string, bookId: string) => void; query: string; setQuery: (value: string) => void; statusFilter: 'all' | 'in-progress' | 'not-started' | 'completed'; setStatusFilter: (value: 'all' | 'in-progress' | 'not-started' | 'completed') => void; sortBy: 'title' | 'author' | 'duration' | 'rating' | 'added'; setSortBy: (value: 'title' | 'author' | 'duration' | 'rating' | 'added') => void; sortDirection: 'asc' | 'desc'; setSortDirection: (value: 'asc' | 'desc') => void; error: string; loading: boolean; scan: ScanStatus | null; onRefresh: () => void; onOpen: (book: Book) => void; onPlay: (book: Book) => void }) {
  const seenRecentAlbums = new Set<string>()
  const recentBooks = features.history.flatMap(item => {
    const book = allBooks.find(entry => entry.id === item.book_id)
    return book ? [{ item, book }] : []
  }).filter(({ book }) => {
    const albumKey = book.id
    if (seenRecentAlbums.has(albumKey)) return false
    seenRecentAlbums.add(albumKey)
    return true
  }).slice(0, 4)
  return <section className="library-page">
    <div className="hero"><div><div className="eyebrow"><BookOpen size={14} /> YOUR COLLECTION</div><h1>Your library</h1><p>Stories for wherever the day takes you.</p></div><div className="hero-art"><Headphones size={88} strokeWidth={1.1} /></div></div>
    <div className="library-heading"><div className="library-heading-copy"><h2>All audiobooks</h2><span>{books.length.toLocaleString()} titles</span></div><label className="search-box"><Search size={17} /><Input placeholder="Search your library" value={query} onChange={event => setQuery(event.target.value)} /></label></div>
    <div className="group-toolbar" role="group" aria-label="Browse audiobooks by"><span className="browse-label">Browse by</span>{(['album-artist', 'album', 'artist', 'directory'] as const).map(mode => <button key={mode} className={groupMode === mode ? 'active' : ''} aria-pressed={groupMode === mode} onClick={() => setGroupMode(mode)}>{mode === 'album-artist' ? 'Album Artist' : mode[0].toUpperCase() + mode.slice(1)}</button>)}</div>
    <details className="filter-panel"><summary><SlidersHorizontal size={16} /><span>Filters and sort</span><span className="filter-summary-state">{statusFilter !== 'all' || favoritesOnly || sortBy !== 'title' ? 'Applied' : 'Optional'}</span></summary><div className="filter-panel-content"><div className="filter-options" role="group" aria-label="Filter audiobooks">{(['all', 'in-progress', 'not-started', 'completed'] as const).map(value => <button key={value} className={`filter-chip ${statusFilter === value ? 'active' : ''}`} aria-pressed={statusFilter === value} onClick={() => setStatusFilter(value)}>{value === 'all' ? 'All books' : value === 'in-progress' ? 'In progress' : value === 'not-started' ? 'Not started' : 'Finished'}</button>)}<button className={`filter-chip ${favoritesOnly ? 'active' : ''}`} aria-pressed={favoritesOnly} onClick={() => setFavoritesOnly(!favoritesOnly)}>Favorites</button></div><label className="sort-control"><span>Sort by</span><select value={sortBy} onChange={event => setSortBy(event.target.value as typeof sortBy)} aria-label="Sort audiobooks"><option value="title">Title</option><option value="author">Author</option><option value="duration">Duration</option><option value="rating">Rating</option><option value="added">Added</option></select><select value={sortDirection} onChange={event => setSortDirection(event.target.value as typeof sortDirection)} aria-label="Sort direction"><option value="asc">Ascending</option><option value="desc">Descending</option></select></label></div></details>
    {featureError && <div className="empty-state">{featureError}</div>}
    {books.some(book => (book.progress?.position ?? 0) > 0) && <><div className="tracks-heading"><div><h2>Continue listening</h2><p>Your saved place</p></div></div><div className="resume-shelf">{books.filter(book => (book.progress?.position ?? 0) > 0).slice(0, 5).map(book => { const track = book.tracks.find(item => item.id === book.progress?.track_id); return track ? <button key={book.id} onClick={() => onPlay(book)}><Cover src={book.cover || '/icon.svg'} alt="" /><span><strong>{book.title}</strong><small>{track.title || track.name} · {duration(book.progress?.position ?? 0)} listened</small></span></button> : null })}</div></>}
    {storage && <p className="storage-summary">{storage.tracks.toLocaleString()} files · {formatBytes(storage.source_bytes)} in Drive · {formatBytes(storage.cached_bytes)} cached · {storage.formats.map(item => `${item.mime_type.replace('audio/', '').replace('application/', '')} ${item.count}`).join(' · ')}</p>}
    {features.playlists.length > 0 && <div className="playlist-shelf"><strong>Playlists</strong><button className={!playlistView ? 'active' : ''} onClick={() => onPlaylistView(null)}>All books</button>{features.playlists.map(playlist => <span key={playlist.id}><button className={playlistView === playlist.id ? 'active' : ''} onClick={() => onPlaylistView(playlist.id)}>{playlist.name} · {playlist.book_ids.length}</button><button aria-label={`Delete ${playlist.name}`} onClick={() => { if (window.confirm(`Delete playlist “${playlist.name}”?`)) void api(`/api/playlists/${playlist.id}`, { method: 'DELETE' }).then(reloadFeatures) }}>×</button></span>)}<button onClick={onCreatePlaylist}><Plus size={14} /> New playlist</button></div>}
    {!features.playlists.length && <button className="create-playlist" onClick={onCreatePlaylist}><Plus size={14} /> Create playlist</button>}
    {features.history.length > 0 && <><div className="tracks-heading"><div><h2>Recently played</h2><p>Pick up where you left off</p></div></div><div className="media-grid recent-grid">{recentBooks.map(({ book }, index) => <MediaCard key={book.id} book={book} index={index} favorite={features.favorites.includes(book.id)} rating={features.ratings[book.id] ?? 0} tags={features.tags[book.id] ?? []} playlists={features.playlists} onFavorite={() => onFeature(book.id, 'favorite', !features.favorites.includes(book.id))} onRate={rating => onFeature(book.id, 'rating', rating)} onTag={() => { const tag = window.prompt('Add a tag'); if (tag?.trim()) onFeature(book.id, 'tags', [...(features.tags[book.id] ?? []), tag.trim()]) }} onAddToPlaylist={onAddToPlaylist} onOpen={() => onOpen(book)} onPlay={() => onPlay(book)} />)}</div></>}
    {!loading && !error && <div className="results-count" aria-live="polite">Showing {books.length} {books.length === 1 ? 'audiobook' : 'audiobooks'}</div>}
    {scan && <ScanProgress scan={scan} onRefresh={onRefresh} />}
    {error && <div className="empty-state">{error} <a href="/auth/google">Sign in</a></div>}
    {loading ? <LibrarySkeleton /> : !error && <GroupGrid books={books} mode={groupMode} sortBy={sortBy} sortDirection={sortDirection} ratings={features.ratings} onOpenGroup={onOpenGroup} />}
    {!loading && !error && books.length === 0 && <div className="empty-state">{query || statusFilter !== 'all' ? 'No audiobooks match these filters.' : 'No audiobooks found. Refresh after you sign in.'}</div>}
    {features.history.length > 0 && <div className="tracks-heading"><div><h2>Listening history</h2><p>Recent activity</p></div></div>}
    {features.history.length > 0 && <div className="history-list">{features.history.slice(0, 10).map((item, index) => { const book = allBooks.find(entry => entry.id === item.book_id); const track = book?.tracks.find(entry => entry.id === item.track_id); return book && track ? <button key={`${item.book_id}-${item.played_at}-${index}`} onClick={() => onPlay(book)}><span>{book.title}</span><small>{track.title || track.name} · {new Date(item.played_at.endsWith('Z') ? item.played_at : item.played_at.replace(' ', 'T') + 'Z').toLocaleString()}</small></button> : null })}</div>}
  </section>
}

function GroupGrid({ books, mode, sortBy, sortDirection, ratings, onOpenGroup }: { books: Book[]; mode: 'artist' | 'directory' | 'album' | 'album-artist'; sortBy: 'title' | 'author' | 'duration' | 'rating' | 'added'; sortDirection: 'asc' | 'desc'; ratings: Record<string, number>; onOpenGroup: (mode: 'artist' | 'directory' | 'album' | 'album-artist', name: string) => void }) {
  const direction = sortDirection === 'asc' ? 1 : -1
  const orderedBooks = [...books].sort((a, b) => {
    const primary = sortBy === 'author' ? (a.artist ?? '').localeCompare(b.artist ?? '')
      : sortBy === 'duration' ? a.duration - b.duration
        : sortBy === 'rating' ? (ratings[a.id] ?? 0) - (ratings[b.id] ?? 0)
          : sortBy === 'added' ? (a.added_at ?? '').localeCompare(b.added_at ?? '')
            : (a.album?.trim() || a.title).localeCompare(b.album?.trim() || b.title)
    return direction * (primary || a.title.localeCompare(b.title))
  })
  if (mode === 'album') return <div className="media-grid">{orderedBooks.map(book => <button className="group-card" key={book.id} onClick={() => onOpenGroup('album', book.album?.trim() || book.title)}><Cover className="media-cover" src={book.cover || '/icon.svg'} alt="" /><strong>{book.album?.trim() || book.title}</strong><span>{book.artist || 'Unknown artist'}</span></button>)}</div>
  const groups = new Map<string, Book[]>()
  for (const book of orderedBooks) {
    const path = book.directory || book.tracks[0]?.path || book.tracks[0]?.name || ''
    const key = mode === 'album-artist' ? book.album_artist?.trim() || book.artist?.trim() || 'Unknown artist' : mode === 'artist' ? book.artist?.trim() || 'Unknown artist' : path.split('/')[0] || 'Root'
    groups.set(key, [...(groups.get(key) ?? []), book])
  }
  const sortedGroups = [...groups.entries()].sort(([nameA, membersA], [nameB, membersB]) => {
    const primary = sortBy === 'duration' ? membersA.reduce((sum, book) => sum + book.duration, 0) - membersB.reduce((sum, book) => sum + book.duration, 0)
      : sortBy === 'rating' ? Math.max(...membersA.map(book => ratings[book.id] ?? 0)) - Math.max(...membersB.map(book => ratings[book.id] ?? 0))
        : sortBy === 'added' ? (membersA[0]?.added_at ?? '').localeCompare(membersB[0]?.added_at ?? '')
          : sortBy === 'author' ? nameA.localeCompare(nameB) : nameA.localeCompare(nameB)
    return direction * primary
  })
  return groups.size ? <div className="media-grid">{sortedGroups.map(([name, members]) => <button className="group-card" key={name} onClick={() => onOpenGroup(mode, name)}><Cover className="media-cover" src={members[0].cover || '/icon.svg'} alt="" /><strong>{name}</strong><span>{members.length} {members.length === 1 ? 'album' : 'albums'}</span></button>)}</div> : <div className="empty-state">No groups found.</div>
}

function GroupPage({ books, onOpen, onOpenGroup }: { books: Book[]; onOpen: (book: Book) => void; onOpenGroup: (mode: 'artist' | 'directory' | 'album' | 'album-artist', name: string) => void }) {
  const { mode = '' } = useParams()
  const [params] = useSearchParams()
  const name = params.get('name') || ''
  const groupMode = mode === 'artist' || mode === 'directory' || mode === 'album' || mode === 'album-artist' ? mode : 'album-artist'
  const isArtistGroup = groupMode === 'artist' || groupMode === 'album-artist'
  const members = books.filter(book => isArtistGroup ? ((groupMode === 'album-artist' ? book.album_artist?.trim() || book.artist?.trim() : book.artist?.trim()) || 'Unknown artist') === name : groupMode === 'album' ? (book.album?.trim() || book.title) === name : (book.directory || book.tracks[0]?.path || book.tracks[0]?.name || '').startsWith(name ? `${name}/` : ''))
  const children = groupMode === 'directory' ? [...new Set(members.map(book => (book.directory || '').slice(name ? name.length + 1 : 0)).filter(path => path.includes('/')).map(path => path.split('/')[0]))] : isArtistGroup ? [...new Set(members.map(book => book.album?.trim()).filter((album): album is string => Boolean(album)))] : []
  const albums = groupMode === 'directory' ? members.filter(book => !(book.directory || '').slice(name ? name.length + 1 : 0).includes('/')) : isArtistGroup && children.length ? members.filter(book => book.album?.trim() === name) : members
  const title = name || (isArtistGroup ? 'Artists' : groupMode === 'album' ? 'Albums' : 'Directories')
  return <section className="library-page"><Link className="back-link" to="/library"><ArrowLeft size={17} /> Your library</Link><div className="tracks-heading"><div><div className="eyebrow">{isArtistGroup ? groupMode === 'album-artist' ? 'ALBUM ARTIST' : 'ARTIST' : groupMode === 'album' ? 'ALBUM' : 'DIRECTORY'}</div><h1>{title}</h1><p>{albums.length} {albums.length === 1 ? 'album' : 'albums'}{children.length ? ` · ${children.length} folders` : ''}</p></div></div>{children.length > 0 && <div className="media-grid">{children.map(child => { const path = groupMode === 'directory' ? name ? `${name}/${child}` : child : child; const cover = members.find(book => isArtistGroup ? book.album === child : book.directory === path)?.cover; return <button className="group-card" key={path} onClick={() => onOpenGroup(groupMode === 'artist' || groupMode === 'album-artist' ? 'album' : 'directory', path)}><Cover className="media-cover" src={cover || '/icon.svg'} alt="" /><strong>{child}</strong><span>{isArtistGroup ? 'Album' : 'Folder'}</span></button> })}</div>}<div className="media-grid">{albums.map(book => <button className="group-card" key={book.id} onClick={() => onOpen(book)}><Cover className="media-cover" src={book.cover || '/icon.svg'} alt={`${book.title} cover`} /><strong>{book.title}</strong><span>{book.artist || book.directory || 'Audiobook'}</span></button>)}</div>{members.length === 0 && <div className="empty-state">No audiobooks found in this group.</div>}</section>
}

function SettingsPage({ reloadLibrary }: { reloadLibrary: () => void }) {
  const [folders, setFolders] = useState<{ id: string; name: string; path: string }[]>([])
  const [selected, setSelected] = useState<string[]>([])
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [saved, setSaved] = useState(false)
  useEffect(() => {
    void Promise.all([api<{ excluded_directories: typeof folders }>('/api/settings/excluded-directories'), api<typeof folders>('/api/directories')])
      .then(([settings, list]) => { setFolders(list); setSelected(settings.excluded_directories.map(item => item.path)) })
      .catch(e => setError(e instanceof Error ? e.message : 'Could not load Drive folders.'))
      .finally(() => setLoading(false))
  }, [])
  const save = async () => {
    setSaving(true); setError(''); setSaved(false)
    try {
      await api('/api/settings/excluded-directories', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ paths: selected }) })
      setSaved(true); reloadLibrary()
    } catch (e) { setError(e instanceof Error ? e.message : 'Could not save exclusions.') }
    finally { setSaving(false) }
  }
  return <section className="library-page settings-page"><div className="eyebrow"><Settings2 size={14} /> PREFERENCES</div><h1>Scan settings</h1><p className="settings-intro">Choose Drive folders to skip. Saving removes matching audio from your library and saved progress.</p>
    {loading ? <LibrarySkeleton /> : <Card className="folder-picker"><h2>Google Drive folders</h2><p>Folders found in the scanned library. Select folders to exclude. Drive listing is not required.</p>{folders.length ? folders.map(folder => <label className="folder-option" key={folder.id}><input type="checkbox" checked={selected.includes(folder.path)} onChange={event => setSelected(current => event.target.checked ? [...current, folder.path] : current.filter(path => path !== folder.path))} /><span>{folder.path}</span></label>) : <p>No subfolders found.</p>}</Card>}
    {error && <p className="settings-error" role="alert">{error}</p>}{saved && <p className="settings-saved" role="status">Exclusions saved. Start a scan to refresh the library.</p>}
    <Button disabled={loading || saving} onClick={() => void save()}>{saving ? 'Saving…' : 'Save exclusions'}</Button>
  </section>
}

function ScanProgress({ scan, onRefresh }: { scan: ScanStatus; onRefresh: () => void }) {
  if (scan.status === 'idle') return null
  const running = scan.status === 'running'
  const done = scan.status === 'completed'
  const percent = scan.total ? Math.min(100, Math.round(scan.processed / scan.total * 100)) : 0
  return <Card className={`scan-card ${running ? 'scan-running' : done ? 'scan-done' : 'scan-error'}`} role="status" aria-live="polite">
    <div className="scan-icon">{running ? <LoaderCircle className="spin" size={18} /> : <CheckCircle2 size={18} />}</div>
    <div className="scan-content"><div className="scan-topline"><strong>{running ? 'Updating your library' : done ? (scan.error ? 'Drive scan complete with errors' : 'Library is up to date') : 'Scan needs attention'}</strong><span>{scan.total ? `${percent}%` : running ? 'Preparing' : scan.status}</span></div>
      <div className="scan-bar"><span className={scan.total ? '' : 'indeterminate'} style={scan.total ? { width: `${percent}%` } : undefined} /></div>
      <div className="scan-subline"><span>{scan.error || (scan.total ? `${scan.processed.toLocaleString()} of ${scan.total.toLocaleString()} files checked` : scan.current || 'Connecting to Google Drive')}</span>{running && scan.total > 0 && <span>{Math.max(0, scan.total - scan.processed).toLocaleString()} left</span>}</div>
      {running && scan.current && scan.total > 0 && <div className="scan-current" title={scan.current}>{scan.current}</div>}
      {!running && !done && <button className="scan-retry" onClick={onRefresh}>Try again</button>}
    </div>
  </Card>
}

function LibrarySkeleton() {
  return <div className="media-grid" aria-label="Loading your library" aria-busy="true">{Array.from({ length: 12 }, (_, index) => <Card className="media-card media-skeleton" key={index}><Skeleton className="skeleton-cover" /><Skeleton className="skeleton-line" /><Skeleton className="skeleton-line short" /></Card>)}</div>
}

function BookSkeleton() {
  return <div className="book-skeleton" aria-label="Loading audiobook" aria-busy="true"><Skeleton className="skeleton-book-cover" /><div className="book-skeleton-lines"><Skeleton className="skeleton-line short" /><Skeleton className="skeleton-line title-line" /><Skeleton className="skeleton-line" /><Skeleton className="skeleton-action" /></div><Skeleton className="skeleton-track" /><Skeleton className="skeleton-track" /></div>
}

function MediaCard({ book, index, favorite, rating, tags, playlists, onFavorite, onRate, onTag, onAddToPlaylist, onOpen, onPlay }: { book: Book; index: number; favorite: boolean; rating: number; tags: string[]; playlists: Features['playlists']; onFavorite: () => void; onRate: (rating: number) => void; onTag: () => void; onAddToPlaylist: (playlistId: string, bookId: string) => void; onOpen: () => void; onPlay: () => void }) {
  const percent = Math.round((book.progress?.fraction ?? 0) * 100)
  return <Card className="media-card media-card-enter" style={{ '--card-index': Math.min(index, 12) } as CSSProperties}><button className="cover-action" onClick={onOpen} aria-label={`Open ${book.title}`}><Cover className="media-cover" src={book.cover || '/icon.svg'} alt={`${book.title} cover`} /><span className="hover-play" onClick={event => { event.stopPropagation(); onPlay() }}><PlayIcon /></span></button>
    <button className="favorite-toggle" aria-label={favorite ? 'Remove favorite' : 'Add favorite'} aria-pressed={favorite} onClick={onFavorite}><Heart size={16} fill={favorite ? 'currentColor' : 'none'} /></button>
    <button className="media-title" onClick={onOpen}>{book.title}</button><div className="media-artist">{book.artist || 'Audiobook'}</div>
    <div className="media-meta"><span>{book.track_count ?? book.tracks.length} {(book.track_count ?? book.tracks.length) === 1 ? 'part' : 'parts'}</span><span>{duration(book.duration)}</span></div>
    {rating > 0 && <div className="book-rating" aria-label={`Rating: ${rating} out of 5`}>{Array.from({ length: rating }, (_, i) => <Star size={12} fill="currentColor" key={i} />)}</div>}
    {tags.length > 0 && <div className="book-tags">{tags.map(tag => <span key={tag}>{tag}</span>)}</div>}
    {book.progress && <div className="resume-indicator">Continue · {Math.floor(book.progress.position / 60)} min</div>}
    {book.progress && <div className="book-progress-bar"><span style={{ width: `${percent}%` }} /></div>}
    <div className="card-tools"><button onClick={onTag}>Tag</button><select aria-label={`Rate ${book.title}`} value={rating} onChange={event => onRate(Number(event.target.value))}><option value={0}>Rate</option>{[1, 2, 3, 4, 5].map(value => <option key={value} value={value}>{value} star{value === 1 ? '' : 's'}</option>)}</select>{playlists.length > 0 && <select aria-label={`Add ${book.title} to playlist`} value="" onChange={event => { if (event.target.value) onAddToPlaylist(event.target.value, book.id) }}><option value="">Add to…</option>{playlists.map(playlist => <option key={playlist.id} value={playlist.id}>{playlist.name}</option>)}</select>}</div>
  </Card>
}

function BookPage({ onPlay }: { onPlay: (book: Book, track: Track, start?: number) => void }) {
  const { bookId = '' } = useParams()
  const [book, setBook] = useState<Book | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [metadataBusy, setMetadataBusy] = useState(false)
  const [metadataError, setMetadataError] = useState('')
  const [candidates, setCandidates] = useState<MetadataCandidate[]>([])
  const [selectedCandidate, setSelectedCandidate] = useState<string | null>(null)
  useEffect(() => {
    setLoading(true)
    setBook(null)
    setError('')
    void api<Book>(`/api/books/${encodeURIComponent(bookId)}`).then(setBook).catch(e => setError(e instanceof Error ? e.message : 'Could not load book.')).finally(() => setLoading(false))
  }, [bookId])
  const searchMetadata = async () => {
    setMetadataBusy(true); setMetadataError(''); setCandidates([]); setSelectedCandidate(null)
    try {
      const result = await api<{ candidates: MetadataCandidate[]; source_errors?: string[] }>(`/api/books/${encodeURIComponent(bookId)}/metadata/search`, { method: 'POST' })
      setCandidates(result.candidates)
      if (result.source_errors?.length) setMetadataError(`Some sources failed: ${result.source_errors.join(', ')}.`)
      else if (!result.candidates.length) setMetadataError('No matches found.')
    } catch (e) { setMetadataError(e instanceof Error ? e.message : 'Metadata search failed.') }
    finally { setMetadataBusy(false) }
  }
  const applyMetadata = async () => {
    if (!selectedCandidate) return
    setMetadataBusy(true); setMetadataError('')
    try {
      const updated = await api<Book>(`/api/books/${encodeURIComponent(bookId)}/metadata/apply`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ source: candidates.find(item => item.source_id === selectedCandidate)?.source, source_id: selectedCandidate }) })
      setBook(updated); setCandidates([])
    } catch (e) { setMetadataError(e instanceof Error ? e.message : 'Could not apply metadata.') }
    finally { setMetadataBusy(false) }
  }
  if (error) return <div className="empty-state">{error}</div>
  if (loading) return <BookSkeleton />
  if (!book) return <div className="empty-state">Audiobook not found.</div>
  const totalChapters = book.tracks.reduce((sum, track) => sum + (track.chapter_count ?? 0), 0)
  return <section className="book-page"><Link className="back-link" to="/"><ArrowLeft size={17} /> Your library</Link>
    <div className="book-hero"><Cover className="book-cover" src={book.cover || '/icon.svg'} alt={`${book.title} cover`} /><div className="book-info"><div className="eyebrow">AUDIOBOOK</div><h1>{book.title}</h1><p className="book-author">{book.artist || 'Unknown author'}</p><div className="book-facts"><span><Headphones size={15} />{book.tracks.length} parts</span><span><Clock3 size={15} />{duration(book.duration)}</span><span><BookOpen size={15} />{totalChapters} chapters</span></div><Button className="primary-action" onClick={() => { const track = book.tracks.find(item => item.id === book.progress?.track_id) || book.tracks[0]; if (track) onPlay(book, track, book.progress?.position || 0) }}><PlayIcon /> Listen now</Button></div></div>
    <div className="tracks-heading"><div><h2>Contents</h2><p>{book.tracks.length} {book.tracks.length === 1 ? 'audio file' : 'audio files'}</p></div><Button disabled={metadataBusy} onClick={() => void searchMetadata()}>{metadataBusy ? 'Searching…' : 'Find metadata'}</Button></div>
    {metadataError && <p role="status">{metadataError}</p>}
    {!!candidates.length && <section aria-label="Metadata matches"><h2>Review match</h2>{candidates.map(candidate => <label key={candidate.source_id} style={{ display: 'flex', alignItems: 'center', gap: 12, padding: 12, borderBottom: '1px solid var(--line)' }}><input type="radio" name="metadata-candidate" checked={selectedCandidate === candidate.source_id} onChange={() => setSelectedCandidate(candidate.source_id)} /><span><strong>{candidate.title}</strong><br /><small>{candidate.source} · {candidate.authors.join(', ')}{candidate.published_date ? ` · ${candidate.published_date}` : ''}</small>{candidate.description && <small style={{ display: 'block' }}>{candidate.description.slice(0, 240)}</small>}</span>{candidate.cover && <img src={candidate.cover} alt="" width="48" height="64" />}</label>)}<Button disabled={metadataBusy || !selectedCandidate} onClick={() => void applyMetadata()}>Apply selected metadata</Button></section>}
    <div className="track-list">{book.tracks.map((track, index) => <TrackSection key={track.id} book={book} track={track} index={index} onPlay={onPlay} />)}</div>
  </section>
}

function TrackSection({ book, track, index, onPlay }: { book: Book; track: Track; index: number; onPlay: (book: Book, track: Track, start?: number) => void }) {
  const [chapters, setChapters] = useState<Chapter[]>([])
  const [offset, setOffset] = useState(0)
  const [hasMore, setHasMore] = useState(true)
  const [loading, setLoading] = useState(false)
  const [chapterError, setChapterError] = useState(false)
  const sentinel = useRef<HTMLDivElement>(null)
  const loadMore = useCallback(async () => {
    if (loading || !hasMore) return
    setChapterError(false)
    setLoading(true)
    try {
      const page = await api<{ chapters: Chapter[]; next_offset: number | null }>(`/api/tracks/${encodeURIComponent(track.id)}/chapters?offset=${offset}&limit=50`)
      setChapters(previous => [...previous, ...page.chapters])
      setOffset(page.next_offset ?? offset + page.chapters.length)
      setHasMore(page.next_offset !== null)
    } catch { setChapterError(true) }
    finally { setLoading(false) }
  }, [track.id, offset, loading, hasMore])
  useEffect(() => { setChapters([]); setOffset(0); setHasMore(true) }, [track.id])
  useEffect(() => {
    const target = sentinel.current
    if (!target || !hasMore) return
    const observer = new IntersectionObserver(entries => { if (entries.some(entry => entry.isIntersecting)) void loadMore() }, { rootMargin: '300px' })
    observer.observe(target)
    return () => observer.disconnect()
  }, [loadMore, hasMore])
  return <Card className="track-section"><div className="track-header"><span className="track-index">{String(index + 1).padStart(2, '0')}</span><div className="track-name"><strong>{track.title || track.name}</strong><span>{duration(track.duration)} · {track.chapter_count ?? 0} chapters</span></div><Button className="track-play" aria-label={`Play ${track.title}`} onClick={() => onPlay(book, track)}><PlayIcon /></Button></div>
    <div className="chapter-list">{chapters.map((chapter, i) => <button className="chapter-row" key={`${track.id}-${chapter.number ?? i}-${chapter.start}`} onClick={() => onPlay(book, track, chapter.start)}><span className="chapter-number">{String(chapter.number ?? i + 1).padStart(2, '0')}</span><span className="chapter-title">{chapter.title || `Chapter ${chapter.number ?? i + 1}`}</span><span className="chapter-time">{timestamp(chapter.start)}</span><PlayIcon /></button>)}</div>
    {chapterError && <button className="load-more" onClick={() => void loadMore()}>Could not load chapters. Retry</button>}
    {loading && <div className="chapter-loading" aria-label="Loading chapters" aria-busy="true">{Array.from({ length: 3 }, (_, i) => <Skeleton className="chapter-skeleton" key={i} />)}</div>}
    {hasMore && <div className="chapter-sentinel" ref={sentinel} aria-hidden="true" />}
  </Card>
}

function formatBytes(bytes: number) { return bytes >= 1024 ** 3 ? `${(bytes / 1024 ** 3).toFixed(1)} GB` : bytes >= 1024 ** 2 ? `${(bytes / 1024 ** 2).toFixed(1)} MB` : `${(bytes / 1024).toFixed(0)} KB` }
function duration(seconds: number) { const mins = Math.floor((seconds || 0) / 60); const hrs = Math.floor(mins / 60); return hrs ? `${hrs} hr ${mins % 60} min` : `${mins} min` }
function timestamp(seconds: number) { const mins = Math.floor(seconds / 60); return `${mins}:${String(Math.floor(seconds % 60)).padStart(2, '0')}` }

export default App
