# Frontend API contract

RuangDengar remains a single-user FastAPI/SQLite application. All API resources require
the authenticated browser session and use seconds for durations and positions. API reads
send `Cache-Control: private, no-store`. Unknown `/api/*` paths return JSON 404 responses,
while application routes continue to serve the React client.

## Catalog and playback

- `GET /api/v1/books`: a bounded summary page. Parameters: `q` (title, author, tags),
  `sort=title|author|duration|rating|added`, `direction=asc|desc`,
  `status=all|not-started|in-progress|completed`, `favorite=true`, `playlist_id`,
  `limit=1..100` (default 50), and `cursor`.
- `GET /api/v1/books/{id}`: canonical detail with ordered track summaries and personal
  state. `/api/books/{id}` and metadata-apply results use the same response model.
- `GET /api/v1/books/{id}/playback`: ordered tracks, checkpoint, and current revision.
- `GET /api/v1/tracks/{id}/chapters?offset=0&limit=50`: direct database page, maximum
  100 chapters, with `next_offset` and a revision. Restart chapter paging if the revision
  changes. The legacy chapter route has the same response.

Summary pages have `items`, `next_cursor`, `revision`, and `total`. Summaries omit tracks;
detail and compatibility library responses contain track summaries with empty chapter
arrays and a chapter count. Provider/probe internals and override storage are not exposed.
Sort ties use book IDs. A cursor is bound to its filters and revision; changed state returns
409 and the client restarts at the first page. The revision currently covers both catalog
and personal-state changes, favoring consistency over long-lived pagination cursors.

Books have durable `added_at` and `track_count`. Tracks have a persisted `ordinal`.
Personal state contains favorites, ratings, tags, and progress. Progress includes
`book_position`, nullable `fraction`, explicit `completed`, `status`, `updated_at`, and
`revision`. A missing track duration makes the whole-book fraction unknown rather than
claiming an accurate percentage.

`GET /api/v1/bootstrap` returns a maximum of 20 summaries, their next cursor, scan status,
and capabilities. The current browser's grouped library uses `GET /api/bootstrap`, a
single SQLite snapshot of the compact full collection index, features, source/cache totals,
scan status, and revision. This preserves complete author/album/directory groups; it is
not a fully paginated group browser. Search uses the paginated v1 catalog. Migrating those
groups to a dedicated bounded group resource is a separate UI change.

## Conditional progress

Read `GET /api/v1/books/{id}/progress` (null means revision 0), then write:

```json
{
  "track_id": "track-id",
  "position": 32,
  "base_revision": 0,
  "event_id": "a-unique-checkpoint-id",
  "completed": false
}
```

PUT uses the same path. The response is the complete accepted checkpoint and increments
its revision. Checking the base revision and writing happen in one transaction. A stale
write returns 409 with `code=progress_conflict` and the current revision. Intentional
backward seeks work when based on the current revision; position is not a monotonic
counter. Known track duration allows two seconds of tolerance; unknown duration permits
finite nonnegative positions up to the request's maximum.

Retries use the same event ID and identical semantic payload. An accepted retry returns
the original accepted result without another write. Reusing an ID for a different payload
returns 409. The server keeps the last 1,000 accepted IDs per book; very old retries outside
that window are still subject to the base-revision check. Removed books remove their IDs.

The legacy `PUT /api/progress/{id}` also requires `base_revision` and `event_id`; absent
preconditions return 428. Older open clients must reload after upgrade. The React client
serializes writes per book, uses acknowledged revisions, and never rebases a stale offline
checkpoint onto a newly fetched remote state. A conflict pauses playback and asks the
listener to resume from the saved state. Old unversioned local checkpoints cannot overwrite
revised server state. Completion is persisted when the final track reaches its end; a replay
checkpoint may explicitly clear it. Active sleep timers remain device-local.

## Playlists, history, scan, and storage

- `PUT /api/v1/playlists/{playlist}/books/{book}` adds membership idempotently.
- `DELETE` at the same path removes it idempotently. Both return the playlist and revision.
- `PUT /api/v1/playlists/{playlist}/books` replaces/reorders unique IDs with
  `{"book_ids":[...],"base_revision":N}`. Stale revisions return 409; missing revisions
  return 428. The existing non-v1 routes support these same operations. Item changes are
  serialized in a SQLite transaction and playlists retain the 1,000-book limit.
- `POST /api/history/{book}` records a play-start event with `track_id` and actual
  starting `position`. It returns 204. The browser awaits it before refreshing history.
  These are recent play-start events, not listening-time analytics.
- `GET /api/v1/history?limit=50&cursor=N` pages events by descending monotonic event ID.
  The response has `items` and `next_cursor`. Retention remains the newest 1,000 events.
- `POST /api/v1/scan-jobs` starts a scan (202) or reports one already running (409).
  Status includes a `job_id`; each new run gets a new ID. Read
  `/api/v1/scan-jobs/current` and subscribe to `/api/v1/scan-jobs/current/events`.
  The existing scan routes remain supported. Library GET no longer starts scans;
  `refresh=true` returns 405 after configuration validation. Sign-in still starts the
  initial scan. The frontend reloads catalog data on completion, not every 50 processed files.
- `GET /api/v1/storage` separates `source_bytes`, `cached_bytes`, and `cache_budget_bytes`.
  `bytes` remains a compatibility alias for source size. `cache_enabled` describes warming
  availability. Cache hits may still serve audio when warming is disabled.
- `POST /api/tracks/{id}/warm` returns `queued` (202), or `available`, `disabled`, `busy`,
  or `too_large` (200). It is no longer an ambiguous empty response.

HTTP exceptions retain `detail` and add a stable `code`. Request-validation errors retain
FastAPI's field-level details. OpenAPI has explicit book, track, chapter, progress,
playlist, history, storage, and bootstrap response models; provider-specific metadata
remains behind normalized field projections. Open Library descriptions, author references,
and publication dates are normalized before applying overrides, and detail exposes source
identity without private probe diagnostics.

## Database upgrade

Stop the service and back up the database before first startup with this version. Upgrade
adds columns/indexes and backfills chapters in a transaction. Existing books receive one
migration-time added timestamp because the old schema did not record discovery time.
Subsequent scans preserve it. Existing last-track checkpoints at the previous 95% completion
threshold are migrated to explicit completion once.

Publication now upserts surviving identities and deletes only genuinely vanished records.
Chapters and accepted progress events have enabled foreign keys with cascade deletion;
legacy personal-state tables keep the explicit reconciliation introduced by the backend
fixes, rather than undergoing a broad table rebuild. Empty and removed catalogs are still
reconciled in the final publication transaction. Chapters no longer live in both public
book and track JSON. Private raw probe metadata may retain source chapters for recovery
from a failed re-probe; it is not read for chapter paging or sent to clients.

Back up before upgrading and restore the backup for rollback: the old application expects
chapters in its JSON records. Do not roll back only the executable against the migrated
database. Run one process/worker per database/cache as documented in DEPLOYMENT.md.
