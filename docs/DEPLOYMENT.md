# Deploy RuangDengar

RuangDengar is a single-user private audiobook player. Run it behind HTTPS and keep app data and OAuth credentials on the server.

## Requirements

- Python 3.11+
- Node.js and npm to build the frontend
- Google Drive API enabled in Google Cloud
- Google OAuth **Web application** client
- An audiobook folder in Google Drive
- HTTPS reverse proxy to the app's Uvicorn port

## Google OAuth

Add this exact redirect URI to the OAuth Web client:

```text
https://YOUR_HOST/auth/callback
```

The application requests read-only Drive access. The allowed user is set with `APP_ALLOWED_EMAIL`.

## Configure and run

Set these variables in the service environment:

```sh
APP_ALLOWED_EMAIL=you@example.com
APP_SECRET_KEY=<at least 32 random characters>
APP_BASE_URL=https://YOUR_HOST
GOOGLE_CLIENT_SECRETS=/run/secrets/google-oauth-web.json
AUDIOBOOKS_FOLDER_ID=<Drive folder ID>
APP_DB_PATH=data/ruangdengar.sqlite3
APP_CACHE_PATH=data/media-cache
APP_CACHE_MAX_BYTES=32212254720
APP_CACHE_WARM_ENABLED=true
```

Create the secret with `openssl rand -hex 32`. Make sure the service user can read the OAuth file and write to the database and cache directories. Keep those paths private.

Build the frontend and start the service:

```sh
npm ci
npm run build
uv sync --extra web
APP_HOST=0.0.0.0 APP_PORT=8111 uv run ruangdengar-web
```

Configure the HTTPS proxy to forward requests to the Uvicorn port. Set trusted proxy headers for the proxy IP only. Use a process manager that restarts the service after failure and host reboot.

The first library scan starts after sign-in. A new scan after a completed scan lists Drive folders and files again, then refreshes file metadata and discovers added audiobooks. If a scan stops or the app restarts during a scan, the next scan resumes from saved folder and file checkpoints. Directory exclusions use paths already indexed by the app and remove matching library data when saved.

## Cache behavior

The cache stores fully downloaded audio tracks, verifies them against Drive metadata, and evicts least-recently-used files at the configured size limit. Background warming uses up to two workers. Set `APP_CACHE_WARM_ENABLED=false` to disable warming. Cache misses keep using authenticated Drive range streaming.

The cache resides on the app host. Caddy or another proxy cannot serve cached files directly unless it shares the cache volume and has a protected internal file-serving route. Current default setup streams cache hits through Python.

## Update

```sh
git pull
npm ci
npm run build
uv sync --extra web
# Restart the RuangDengar service with its configured environment.
```


## Backend state and upgrade notes

Run one application process/worker per database and cache. Scanner locks, SSE events,
credential-file synchronization, and cache eviction are coordinated within that process;
multiple Uvicorn workers or replicas sharing these paths are not supported.

Before upgrading, stop the service and back up the SQLite database. Keep its parent
directory private to the service user (`chmod 700 data`). The app enforces mode 600 on
its database and writes credential files atomically with private permissions. Backups
contain OAuth secrets and must be protected the same way.

The backend audit fixes migrate existing track-to-book identities and manual metadata
into dedicated tables on startup. Existing valid book IDs are retained when their Drive
tracks are rediscovered. New books receive opaque IDs; folder or album renames retain
identity through the track mapping. Previously empty IDs are repaired automatically.
Previously merged books cannot have their personal state unambiguously divided; when
separated, the existing state follows one matched book and the others get new IDs.
Tagged editions in distinct directories are grouped separately; `Disc 1` / `CD 2`
subfolders under an edition remain one book.

A scan persists per-file checkpoints without replacing the live library. Only its final
publication reconciles books, tracks, and dependent personal state. Interrupted or failed
scans leave the current library available. Removing a book at successful publication
removes its favorites, rating, tags, playlist memberships, history, and progress; manual
metadata overrides and track identity mappings are retained for rediscovery. Removing
just the saved progress track clears that checkpoint. A failed probe of an existing
track retains its last successful metadata and reports the scan error.

Existing browser sessions need to sign in again after this upgrade. Sign-out invalidates
all sessions for this single-user app and fences old credential workers. It does not
revoke the Google OAuth grant; revoke that separately in your Google account if needed.
Cache checksums are verified before new entries are published. Existing cache entries
created before this upgrade should be cleared once, while the service is stopped, to
ensure they were verified by the new writer.

## Frontend API/database upgrade

The frontend API changes add queryable added dates/track ordering, normalized chapter rows,
conditional progress, and playlist revisions. Follow the backup/stop procedure above.
Startup backfills existing books once; their added dates are migration dates because the
old database did not retain discovery timestamps. Publication upserts surviving books.

Deploy the matching frontend build and reload open browser tabs. Progress writes now need
`base_revision` and `event_id`; whole-playlist replacements need `base_revision`. Old clients
receive 428 instead of overwriting newer state. Rollback requires restoring the matching
pre-upgrade database backup because chapter storage changes. See [API.md](API.md) for the
current contracts and compatibility boundaries.
