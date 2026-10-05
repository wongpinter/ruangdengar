"""Private, single-user audiobook web app."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import sqlite3
import tempfile
import threading
import time
from collections.abc import AsyncIterator, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import ExitStack, asynccontextmanager, contextmanager, nullcontext, suppress
from pathlib import Path
from typing import Annotated, Any, BinaryIO

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware
from starlette.types import Receive, Scope, Send

from .api_models import (
    BookPage,
    BookResponse,
    BootstrapResponse,
    BoundedBootstrapResponse,
    ChapterPage,
    FeaturesResponse,
    HistoryPage,
    PlaybackResponse,
    PlaylistBooksResponse,
    PlaylistResponse,
    ProgressResponse,
    ScanResponse,
    StorageResponse,
    WarmResponse,
)
from .catalog import CatalogQueries, bump_revision, migrate_catalog, write_projection
from .models import SOURCE_GDRIVE, Chapter, Cover, TrackMeta
from .naming import group_tracks
from .probe import probe
from .reader import FetchError, SeekableBlockReader
from .sources.base import RemoteFile
from .sources.gdrive import DRIVE_API, DRIVE_READONLY_SCOPE, DriveError, DriveSource
from .sources.http import HttpRangeFetcher

logger = logging.getLogger(__name__)


class RequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProgressBody(RequestBody):
    track_id: StrictStr
    position: Annotated[float, Field(strict=True, ge=0, le=10_000_000, allow_inf_nan=False)] = 0
    base_revision: Annotated[StrictInt, Field(ge=0)] | None = None
    event_id: Annotated[StrictStr, Field(min_length=1, max_length=128)] | None = None
    completed: StrictBool | None = None


class ConditionalProgressBody(ProgressBody):
    base_revision: Annotated[StrictInt, Field(ge=0)]
    event_id: Annotated[StrictStr, Field(min_length=1, max_length=128)]


class FavoriteBody(RequestBody):
    favorite: StrictBool


class RatingBody(RequestBody):
    rating: Annotated[StrictInt, Field(ge=1, le=5)] | None


class TagsBody(RequestBody):
    tags: Annotated[list[StrictStr], Field(max_length=20)]


class PlaylistBody(RequestBody):
    name: Annotated[StrictStr, Field(max_length=80)]


class PlaylistBooksBody(RequestBody):
    book_ids: Annotated[list[StrictStr], Field(max_length=1000)]
    base_revision: Annotated[StrictInt, Field(ge=0)] | None = None


class HistoryBody(RequestBody):
    track_id: StrictStr
    position: Annotated[float, Field(strict=True, ge=0, le=10_000_000, allow_inf_nan=False)] = 0


class ExclusionsBody(RequestBody):
    paths: Annotated[list[StrictStr], Field(max_length=500)]


class MetricsBody(RequestBody):
    startupMs: Annotated[StrictInt, Field(ge=0, le=600_000)]
    stalls: Annotated[StrictInt, Field(ge=0, le=10_000)]
    bufferedAhead: Annotated[StrictInt, Field(ge=0, le=86_400)]
    rangeMs: Annotated[StrictInt, Field(ge=0, le=86_400_000)]
    rangeBytes: Annotated[StrictInt, Field(ge=0, le=1_000_000_000_000)]
    ranges: Annotated[StrictInt, Field(ge=0, le=100_000)]


class MetadataBody(RequestBody):
    source: StrictStr
    source_id: Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")]


class CachedMediaStream(Iterator[bytes]):
    """An eagerly opened cache handle owned by one response."""

    def __init__(self, source: BinaryIO, start: int, length: int) -> None:
        self.source = source
        self.source.seek(start)
        self.remaining = length

    def __next__(self) -> bytes:
        if self.remaining <= 0:
            self.close()
            raise StopIteration
        chunk = self.source.read(min(256 * 1024, self.remaining))
        if not chunk:
            self.close()
            raise StopIteration
        self.remaining -= len(chunk)
        return chunk

    def close(self) -> None:
        self.source.close()


class CachedResponse(StreamingResponse):
    def __init__(self, stream: CachedMediaStream, **kwargs: Any) -> None:
        self.stream = stream
        super().__init__(stream, **kwargs)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            # Includes a disconnect before the iterator's first read.
            self.stream.close()


class MediaCache:
    """Bounded cache for complete Drive media files."""

    def __init__(self, path: Path, max_bytes: int) -> None:
        self.path = path
        self.max_bytes = max(0, max_bytes)
        self.lock = threading.RLock()
        self.path.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.chmod(0o700)

    @staticmethod
    def _key(track: dict[str, Any]) -> str:
        identity = str(track.get("md5") or f"{track['id']}-{track.get('modified') or 'unknown'}")
        suffix = Path(str(track["name"])).suffix.lower()
        suffix = suffix if re.fullmatch(r"\.[a-z0-9]{1,8}", suffix) else ".audio"
        return f"{track['id']}-{identity}{suffix}"

    def file(self, track: dict[str, Any]) -> Path | None:
        path = self.path / self._key(track)
        try:
            if path.stat().st_size != int(track.get("size") or 0):
                return None
            os.utime(path, None)
            return path
        except OSError:
            return None

    def put(self, track: dict[str, Any], chunks: Iterator[bytes]) -> Path | None:
        size = int(track.get("size") or 0)
        if self.max_bytes <= 0 or size <= 0 or size > self.max_bytes:
            return None
        target = self.path / self._key(track)
        temp = self.path / f".{target.name}.{secrets.token_hex(8)}.part"
        try:
            with self.lock, temp.open("xb") as output:
                digest = hashlib.md5(usedforsecurity=False)
                for chunk in chunks:
                    output.write(chunk)
                    digest.update(chunk)
                os.fchmod(output.fileno(), 0o600)
                output.flush()
                os.fsync(output.fileno())
                if temp.stat().st_size != size:
                    return None
                if track.get("md5") and digest.hexdigest() != track["md5"]:
                    logger.warning("Media checksum mismatch track=%s", track["id"])
                    return None
                temp.replace(target)
                self._evict(target)
                return target
        except (OSError, FetchError):
            logger.info("Media cache fill failed track=%s", track.get("id"), exc_info=True)
            return None
        finally:
            temp.unlink(missing_ok=True)

    def _evict(self, newest: Path) -> None:
        files = sorted(
            (
                path
                for path in self.path.iterdir()
                if path.is_file() and not path.name.endswith(".part")
            ),
            key=lambda path: path.stat().st_atime,
        )
        total = sum(path.stat().st_size for path in files)
        for path in files:
            if total <= self.max_bytes:
                break
            if path == newest:
                continue
            size = path.stat().st_size
            path.unlink(missing_ok=True)
            total -= size

    def serve(self, path: Path, start: int, length: int) -> CachedMediaStream:
        with self.lock:
            return CachedMediaStream(path.open("rb"), start, length)


class WebConfig:
    def __init__(self) -> None:
        self.allowed_email = os.getenv("APP_ALLOWED_EMAIL", "").strip().lower()
        self.base_url = os.getenv("APP_BASE_URL", "http://localhost:8000").rstrip("/")
        self.host = self.base_url.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0].lower()
        self.public_base_url = os.getenv("APP_PUBLIC_BASE_URL", self.base_url).rstrip("/")
        self.secret = os.getenv("APP_SECRET_KEY", "")
        self.cookie_secure = self.base_url.startswith("https://")
        self.require_https = self.cookie_secure or self.host in {"localhost", "127.0.0.1", "::1"}
        self.client_secrets = Path(os.getenv("GOOGLE_CLIENT_SECRETS", "client.json"))
        self.folder_id = os.getenv("AUDIOBOOKS_FOLDER_ID", "")
        self.db_path = Path(os.getenv("APP_DB_PATH", "data/audiobooks.sqlite3"))
        self.credentials_path = self.db_path.with_name("credentials.json")
        self.cache_path = Path(
            os.getenv("APP_CACHE_PATH", str(self.db_path.parent / "media-cache"))
        )
        self.cache_max_bytes = max(0, int(os.getenv("APP_CACHE_MAX_BYTES", str(30 * 1024**3))))
        self.cache_warm_enabled = os.getenv("APP_CACHE_WARM_ENABLED", "true").lower() == "true"

    def check(self) -> None:
        if not self.require_https:
            raise RuntimeError("APP_BASE_URL must use HTTPS outside local development")
        if len(self.secret) < 32:
            raise RuntimeError("APP_SECRET_KEY must contain at least 32 characters")
        missing = [
            name
            for name, value in (
                ("APP_ALLOWED_EMAIL", self.allowed_email),
                ("APP_SECRET_KEY", self.secret),
                ("AUDIOBOOKS_FOLDER_ID", self.folder_id),
            )
            if not value
        ]
        if missing:
            raise RuntimeError("Missing required settings: " + ", ".join(missing))


class Database(CatalogQueries):
    def __init__(self, path: Path) -> None:
        self.path = path
        self.auth_lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS auth_state (
                    id INTEGER PRIMARY KEY CHECK(id=1), generation TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS book_overrides (
                    book_id TEXT PRIMARY KEY, data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS track_identity (
                    track_id TEXT PRIMARY KEY, book_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS books (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, artist TEXT,
                    cover TEXT, duration REAL NOT NULL DEFAULT 0, data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tracks (
                    id TEXT PRIMARY KEY, book_id TEXT NOT NULL, name TEXT NOT NULL,
                    path TEXT NOT NULL, size INTEGER NOT NULL DEFAULT 0,
                    mime_type TEXT, data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS progress (
                    book_id TEXT PRIMARY KEY, track_id TEXT NOT NULL,
                    position REAL NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS credentials (
                    id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS scan_state (
                    id INTEGER PRIMARY KEY CHECK(id=1), status TEXT NOT NULL,
                    total INTEGER NOT NULL DEFAULT 0, processed INTEGER NOT NULL DEFAULT 0,
                    current TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS scan_items (
                    id TEXT PRIMARY KEY, data TEXT NOT NULL, error TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS scan_directories (
                    id TEXT PRIMARY KEY, path TEXT NOT NULL,
                    listed INTEGER NOT NULL DEFAULT 0, scanned INTEGER NOT NULL DEFAULT 0,
                    files_listed INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS scan_inventory (
                    id TEXT PRIMARY KEY, path TEXT NOT NULL,
                    listed INTEGER NOT NULL DEFAULT 0, scanned INTEGER NOT NULL DEFAULT 0,
                    files_listed INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS scan_file_inventory (
                    id TEXT PRIMARY KEY, folder_id TEXT NOT NULL, data TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS scan_file_inventory_folder
                    ON scan_file_inventory(folder_id);
                CREATE TABLE IF NOT EXISTS favorites (
                    book_id TEXT PRIMARY KEY
                );
                CREATE TABLE IF NOT EXISTS ratings (
                    book_id TEXT PRIMARY KEY,
                    rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5)
                );
                CREATE TABLE IF NOT EXISTS book_tags (
                    book_id TEXT NOT NULL, tag TEXT NOT NULL,
                    PRIMARY KEY(book_id, tag)
                );
                CREATE TABLE IF NOT EXISTS playlists (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS playlist_books (
                    playlist_id TEXT NOT NULL, book_id TEXT NOT NULL,
                    position INTEGER NOT NULL, PRIMARY KEY(playlist_id, book_id)
                );
                CREATE TABLE IF NOT EXISTS listening_history (
                    id INTEGER PRIMARY KEY, book_id TEXT NOT NULL, track_id TEXT NOT NULL,
                    position REAL NOT NULL DEFAULT 0,
                    played_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS excluded_directories (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, path TEXT NOT NULL
                );
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(scan_directories)")}
            for column in ("listed", "scanned", "files_listed"):
                if column not in columns:
                    db.execute(
                        f"ALTER TABLE scan_directories ADD COLUMN {column} "
                        "INTEGER NOT NULL DEFAULT 0"
                    )
            inventory_columns = {row[1] for row in db.execute("PRAGMA table_info(scan_inventory)")}
            for column in ("listed", "scanned", "files_listed"):
                if column not in inventory_columns:
                    db.execute(
                        f"ALTER TABLE scan_inventory ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0"
                    )
            db.execute(
                "INSERT OR IGNORE INTO scan_inventory(id,path) SELECT id,path FROM scan_directories"
            )

            empty_book = db.execute("SELECT data FROM books WHERE id='' ").fetchone()
            if empty_book:
                new_id = "book-" + secrets.token_hex(16)
                book = json.loads(empty_book["data"])
                book["id"] = new_id
                for track in book.get("tracks", []):
                    track["book_id"] = new_id
                db.execute("UPDATE books SET id=?,data=? WHERE id=''", (new_id, json.dumps(book)))
                for row in db.execute("SELECT id,data FROM tracks WHERE book_id='' ").fetchall():
                    track = json.loads(row["data"])
                    track["book_id"] = new_id
                    db.execute(
                        "UPDATE tracks SET book_id=?,data=? WHERE id=?",
                        (new_id, json.dumps(track), row["id"]),
                    )
                for table in (
                    "progress",
                    "favorites",
                    "ratings",
                    "book_tags",
                    "playlist_books",
                    "listening_history",
                    "book_overrides",
                    "track_identity",
                ):
                    db.execute(f"UPDATE {table} SET book_id=? WHERE book_id=''", (new_id,))
            db.execute("INSERT OR IGNORE INTO auth_state VALUES(1,?)", (secrets.token_hex(32),))
            db.execute("INSERT OR IGNORE INTO track_identity SELECT id,book_id FROM tracks")
            for row in db.execute("SELECT id,data FROM books").fetchall():
                overrides = json.loads(row["data"]).get("metadata_overrides", {})
                if overrides:
                    db.execute(
                        "INSERT OR IGNORE INTO book_overrides VALUES(?,?)",
                        (row["id"], json.dumps(overrides)),
                    )
            migrate_catalog(db)
        self.path.chmod(0o600)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            db.commit()
        finally:
            db.close()

    @contextmanager
    def book_write(self, book_id: str) -> Iterator[sqlite3.Connection]:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM books WHERE id=?", (book_id,)).fetchone() is None:
                raise HTTPException(404, "Book not found")
            yield db

    def credentials(self) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT data FROM credentials WHERE id=1").fetchone()
        if not row:
            return None
        data = json.loads(row["data"])
        return data if data.get("refresh_token") else None

    def auth_generation(self) -> str:
        with self.connect() as db:
            return str(db.execute("SELECT generation FROM auth_state WHERE id=1").fetchone()[0])

    def save_credentials(self, data: str, generation: str | None = None) -> bool:
        with self.auth_lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT generation FROM auth_state WHERE id=1").fetchone()[0]
            if generation is not None and generation != current:
                return False
            db.execute("INSERT OR REPLACE INTO credentials(id,data) VALUES(1,?)", (data,))
        return True

    def sync_credentials(self, path: Path) -> None:
        with self.auth_lock:
            data = self.credentials()
            if data is None:
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(dir=path.parent, prefix=".credentials-")
            temporary = Path(name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as output:
                    json.dump(data, output)
                    output.flush()
                    os.fsync(output.fileno())
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)

    def disconnect(self, path: Path) -> None:
        with self.auth_lock:
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "UPDATE auth_state SET generation=? WHERE id=1", (secrets.token_hex(32),)
                )
                db.execute("DELETE FROM credentials")
            path.unlink(missing_ok=True)

    def connect_account(self, data: str, path: Path) -> str:
        with self.auth_lock:
            self.disconnect(path)
            generation = self.auth_generation()
            self.save_credentials(data, generation)
            self.sync_credentials(path)
            return generation

    def identities(self) -> dict[str, str]:
        with self.connect() as db:
            return dict(db.execute("SELECT track_id,book_id FROM track_identity").fetchall())

    def update_book_metadata(self, book_id: str, metadata: dict[str, Any]) -> dict[str, Any] | None:
        allowed = {
            "title",
            "artist",
            "album_artist",
            "album",
            "description",
            "publisher",
            "published_date",
            "isbn",
            "cover",
            "metadata_source",
        }
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM books WHERE id=?", (book_id,)).fetchone()
            if row is None:
                return None
            book = json.loads(row["data"])
            overrides = book.setdefault("metadata_overrides", {})
            overrides.update({key: value for key, value in metadata.items() if key in allowed})
            book.update(overrides)
            db.execute(
                "INSERT OR REPLACE INTO book_overrides VALUES(?,?)",
                (book_id, json.dumps(overrides)),
            )
            db.execute(
                "UPDATE books SET title=?,artist=?,cover=?,data=? WHERE id=?",
                (book["title"], book.get("artist"), book.get("cover"), json.dumps(book), book_id),
            )
            bump_revision(db)
        return self.book(book_id, include_chapters=False)

    def track(self, track_id: str, include_chapters: bool = False) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT data FROM tracks WHERE id=?", (track_id,)).fetchone()
            track = json.loads(row["data"]) if row else None
            if track is not None and include_chapters:
                track["chapters"] = [
                    json.loads(chapter[0])
                    for chapter in db.execute(
                        "SELECT data FROM chapters WHERE track_id=? ORDER BY ordinal", (track_id,)
                    )
                ]
            return track

    def save_library(self, books: list[dict[str, Any]], tracks: list[dict[str, Any]]) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            overrides = {
                row["book_id"]: json.loads(row["data"])
                for row in db.execute("SELECT book_id,data FROM book_overrides")
            }
            for book in books:
                saved = overrides.get(book["id"], {})
                if saved:
                    book["metadata_overrides"] = saved
                    book.update(saved)
            # Retain identities throughout publication; delete only vanished records.
            write_projection(db, books, tracks)
            db.execute("CREATE TEMP TABLE live_books(id TEXT PRIMARY KEY)")
            db.execute("CREATE TEMP TABLE live_tracks(id TEXT PRIMARY KEY)")
            db.executemany("INSERT INTO live_books VALUES(?)", [(b["id"],) for b in books])
            db.executemany("INSERT INTO live_tracks VALUES(?)", [(t["id"],) for t in tracks])
            db.execute("DELETE FROM tracks WHERE id NOT IN (SELECT id FROM live_tracks)")
            db.execute("DELETE FROM books WHERE id NOT IN (SELECT id FROM live_books)")
            db.execute("DELETE FROM progress_events WHERE book_id NOT IN (SELECT id FROM books)")
            bump_revision(db)

            db.executemany(
                "INSERT OR REPLACE INTO track_identity VALUES(?,?)",
                [(track["id"], track["book_id"]) for track in tracks],
            )
            # Reconcile only after publishing a complete scan generation.
            for table in ("favorites", "ratings", "book_tags", "playlist_books"):
                db.execute(f"DELETE FROM {table} WHERE book_id NOT IN (SELECT id FROM books)")
            db.execute(
                "DELETE FROM progress WHERE NOT EXISTS ("
                "SELECT 1 FROM tracks WHERE tracks.id=progress.track_id "
                "AND tracks.book_id=progress.book_id)"
            )
            db.execute(
                "DELETE FROM listening_history WHERE NOT EXISTS ("
                "SELECT 1 FROM tracks WHERE tracks.id=listening_history.track_id "
                "AND tracks.book_id=listening_history.book_id)"
            )

    def scan_status(self) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM scan_state WHERE id=1").fetchone()
        return dict(row) if row else None

    def save_scan_status(self, **values: Any) -> None:
        fields = ("status", "total", "processed", "current", "error")
        with self.connect() as db:
            row = db.execute("SELECT * FROM scan_state WHERE id=1").fetchone()
            status = (
                dict(row)
                if row
                else {
                    "status": "running",
                    "total": 0,
                    "processed": 0,
                    "current": "",
                    "error": "",
                }
            )
            new_job = values.get("status") == "running" and status.get("status") != "running"
            status.update({key: value for key, value in values.items() if key in fields})
            db.execute(
                "INSERT INTO scan_state (id,status,total,processed,current,error) "
                "VALUES(1,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "status=excluded.status,total=excluded.total,processed=excluded.processed,"
                "current=excluded.current,error=excluded.error,updated_at=CURRENT_TIMESTAMP",
                [status[key] for key in fields],
            )
            if new_job or not status.get("job_id"):
                db.execute(
                    "UPDATE scan_state SET job_id=? WHERE id=1", (secrets.token_urlsafe(16),)
                )

    def pending_scan_items(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT data FROM scan_items").fetchall()
        return [json.loads(row["data"]) for row in rows]

    def save_scan_item(self, item: dict[str, Any], error: str = "") -> None:
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO scan_items(id,data,error) VALUES(?,?,?)",
                (item["id"], json.dumps(item), error),
            )

    def clear_scan_items(self) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM scan_items")
            db.execute("DELETE FROM scan_file_inventory")

    def save_scan_files(self, folder_id: str, files: list[RemoteFile]) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM scan_file_inventory WHERE folder_id=?", (folder_id,))
            db.execute("UPDATE scan_directories SET files_listed=0 WHERE id=?", (folder_id,))
            db.execute("UPDATE scan_inventory SET files_listed=0 WHERE id=?", (folder_id,))
            db.executemany(
                "INSERT OR IGNORE INTO scan_file_inventory(id,folder_id,data) VALUES(?,?,?)",
                [
                    (
                        item.id,
                        folder_id,
                        json.dumps(
                            {
                                "id": item.id,
                                "name": item.name,
                                "path": item.path,
                                "size": item.size,
                                "mime_type": item.mime_type,
                                "modified": item.modified,
                                "md5": item.md5,
                                "extra": item.extra,
                            }
                        ),
                    )
                    for item in files
                ],
            )
            db.execute("UPDATE scan_directories SET files_listed=1 WHERE id=?", (folder_id,))
            db.execute("UPDATE scan_inventory SET files_listed=1 WHERE id=?", (folder_id,))

    def scan_files_for_directory(self, folder_id: str, pending_ids: set[str]) -> list[RemoteFile]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT f.data FROM scan_file_inventory f "
                "LEFT JOIN scan_items i ON i.id=f.id "
                "WHERE f.folder_id=? AND (i.id IS NULL OR i.error != '') ORDER BY f.rowid",
                (folder_id,),
            ).fetchall()
        return [
            remote
            for row in rows
            if (remote := RemoteFile(**json.loads(row["data"]))).id not in pending_ids
        ]

    def scan_file_count(self) -> int:
        with self.connect() as db:
            return int(db.execute("SELECT count(*) FROM scan_file_inventory").fetchone()[0])

    def scan_directories(
        self, *, listed: bool | None = None, scanned: bool | None = None
    ) -> list[tuple[str, str, bool, bool, bool]]:
        conditions = []
        if listed is not None:
            conditions.append(f"listed={int(listed)}")
        if scanned is not None:
            conditions.append(f"scanned={int(scanned)}")
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        with self.connect() as db:
            rows = db.execute(
                f"SELECT id,path,listed,scanned,files_listed FROM scan_directories{where} "
                "ORDER BY rowid"
            ).fetchall()
        return [
            (
                str(row["id"]),
                str(row["path"]),
                bool(row["listed"]),
                bool(row["scanned"]),
                bool(row["files_listed"]),
            )
            for row in rows
        ]

    def queue_scan_directories(self, directories: list[tuple[str, str]]) -> None:
        with self.connect() as db:
            db.executemany(
                "INSERT OR IGNORE INTO scan_directories(id,path) VALUES(?,?)", directories
            )
            db.executemany("INSERT OR IGNORE INTO scan_inventory(id,path) VALUES(?,?)", directories)

    def enqueue_scan_directory(self, folder_id: str, path: str) -> None:
        self.queue_scan_directories([(folder_id, path)])

    def mark_scan_directory(
        self, folder_id: str, *, listed: bool | None = None, scanned: bool | None = None
    ) -> None:
        updates = [
            f"{key}=?"
            for key, value in (("listed", listed), ("scanned", scanned))
            if value is not None
        ]
        values = [int(value) for value in (listed, scanned) if value is not None]
        if updates:
            with self.connect() as db:
                db.execute(
                    f"UPDATE scan_directories SET {', '.join(updates)} WHERE id=?",
                    (*values, folder_id),
                )
                db.execute(
                    f"UPDATE scan_inventory SET {', '.join(updates)} WHERE id=?",
                    (*values, folder_id),
                )

    def mark_scan_files_listed(self, folder_id: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE scan_directories SET files_listed=1 WHERE id=?", (folder_id,))
            db.execute("UPDATE scan_inventory SET files_listed=1 WHERE id=?", (folder_id,))

    def inventory_directories(
        self, *, listed: bool | None = None, scanned: bool | None = None
    ) -> list[tuple[str, str, bool, bool, bool]]:
        conditions = []
        if listed is not None:
            conditions.append(f"listed={int(listed)}")
        if scanned is not None:
            conditions.append(f"scanned={int(scanned)}")
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        with self.connect() as db:
            rows = db.execute(
                f"SELECT id,path,listed,scanned,files_listed FROM scan_inventory{where} "
                "ORDER BY rowid"
            ).fetchall()
        return [
            (
                str(row["id"]),
                str(row["path"]),
                bool(row["listed"]),
                bool(row["scanned"]),
                bool(row["files_listed"]),
            )
            for row in rows
        ]

    def clear_scan_directories(self) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM scan_directories")
            db.execute("DELETE FROM scan_inventory")

    def reset_scan_inventory(self) -> None:
        with self.connect() as db:
            db.execute("UPDATE scan_inventory SET listed=0, scanned=0")
            db.execute("UPDATE scan_directories SET listed=0, scanned=0")

    def finish_scan_directory(self, folder_id: str) -> None:
        self.mark_scan_directory(folder_id, scanned=True)

    def save_progress(self, book_id: str, track_id: str, position: float) -> None:
        # Legacy/internal compatibility; versioned API requires conditional writes.
        self.checkpoint(book_id, track_id, position)

    def feature_data(self, connection: sqlite3.Connection | None = None) -> dict[str, Any]:
        with nullcontext(connection) if connection is not None else self.connect() as db:
            if not db.in_transaction:
                db.execute("BEGIN")
            favorites = [row[0] for row in db.execute("SELECT book_id FROM favorites")]
            ratings = {row[0]: row[1] for row in db.execute("SELECT book_id,rating FROM ratings")}
            tags: dict[str, list[str]] = {}
            for row in db.execute("SELECT book_id,tag FROM book_tags ORDER BY tag COLLATE NOCASE"):
                tags.setdefault(row[0], []).append(row[1])
            playlists = [
                dict(row) for row in db.execute("SELECT * FROM playlists ORDER BY created_at")
            ]
            membership: dict[str, list[str]] = {}
            for row in db.execute(
                "SELECT playlist_id,book_id FROM playlist_books ORDER BY position"
            ):
                membership.setdefault(row[0], []).append(row[1])
            for playlist in playlists:
                playlist["book_ids"] = membership.get(playlist["id"], [])
            history = [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM listening_history ORDER BY played_at DESC, id DESC LIMIT 50"
                )
            ]
        return {
            "favorites": favorites,
            "ratings": ratings,
            "tags": tags,
            "playlists": playlists,
            "history": history,
        }

    def scanned_directories(self) -> list[dict[str, str]]:
        with self.connect() as db:
            paths = [row[0] for row in db.execute("SELECT path FROM tracks")]
        directories = {
            "/".join(parts[:index])
            for path in paths
            if isinstance(path, str)
            for parts in [path.strip("/").split("/")]
            for index in range(1, len(parts))
        }
        directories.update(item["path"] for item in self.excluded_directories())
        return [
            {"id": path, "name": path.rsplit("/", 1)[-1], "path": path}
            for path in sorted(directories, key=str.casefold)
        ]

    def excluded_directories(self) -> list[dict[str, str]]:
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT id,name,path FROM excluded_directories ORDER BY path COLLATE NOCASE"
                )
            ]

    def replace_excluded_directories(self, directories: list[dict[str, str]]) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM excluded_directories")
            db.executemany(
                "INSERT INTO excluded_directories(id,name,path) VALUES(?,?,?)",
                [(item["path"], item["name"], item["path"]) for item in directories],
            )
            prefixes = [item["path"].rstrip("/") + "/" for item in directories]
            for row in db.execute("SELECT id,data FROM books").fetchall():
                book = json.loads(row["data"])
                kept = [
                    track
                    for track in book.get("tracks", [])
                    if not any(track.get("path", "").startswith(prefix) for prefix in prefixes)
                ]
                removed = [track for track in book.get("tracks", []) if track not in kept]
                for track in removed:
                    db.execute("DELETE FROM tracks WHERE id=?", (track["id"],))
                    db.execute("DELETE FROM listening_history WHERE track_id=?", (track["id"],))
                    db.execute(
                        "DELETE FROM progress WHERE book_id=? AND track_id=?",
                        (row["id"], track["id"]),
                    )
                if not kept:
                    db.execute("DELETE FROM progress WHERE book_id=?", (row["id"],))
                    db.execute("DELETE FROM favorites WHERE book_id=?", (row["id"],))
                    db.execute("DELETE FROM ratings WHERE book_id=?", (row["id"],))
                    db.execute("DELETE FROM book_tags WHERE book_id=?", (row["id"],))
                    db.execute("DELETE FROM playlist_books WHERE book_id=?", (row["id"],))
                    db.execute("DELETE FROM books WHERE id=?", (row["id"],))
                elif removed:
                    for ordinal, track in enumerate(kept):
                        track["ordinal"] = ordinal
                        db.execute(
                            "UPDATE tracks SET ordinal=?,data=json_set(data,'$.ordinal',?) "
                            "WHERE id=?",
                            (ordinal, ordinal, track["id"]),
                        )
                    book["tracks"] = kept
                    book["track_count"] = len(kept)
                    book["duration"] = sum(track.get("duration", 0) for track in kept)
                    db.execute(
                        "UPDATE books SET duration=?,data=? WHERE id=?",
                        (book["duration"], json.dumps(book), row["id"]),
                    )
                    db.execute("UPDATE books SET track_count=? WHERE id=?", (len(kept), row["id"]))
            db.execute("DELETE FROM progress_events WHERE book_id NOT IN (SELECT id FROM books)")
            bump_revision(db)

    def record_history(self, book_id: str, track_id: str, position: float = 0) -> None:
        with self.book_write(book_id) as db:
            if (
                db.execute(
                    "SELECT 1 FROM tracks WHERE id=? AND book_id=?", (track_id, book_id)
                ).fetchone()
                is None
            ):
                raise HTTPException(422, "track does not belong to this book")
            db.execute(
                "INSERT INTO listening_history(book_id,track_id,position) VALUES(?,?,?)",
                (book_id, track_id, position),
            )
            db.execute(
                "DELETE FROM listening_history WHERE id NOT IN "
                "(SELECT id FROM listening_history ORDER BY played_at DESC, id DESC LIMIT 1000)"
            )


class StoredDriveAuth:
    """Load and refresh OAuth credentials stored on the server."""

    def __init__(self, db: Database, credentials_path: Path) -> None:
        self.db = db
        self._credentials_path = credentials_path
        self.generation = db.auth_generation()
        self.creds: Any = None
        self._lock = threading.RLock()

    def _load(self) -> Any:
        from google.oauth2.credentials import Credentials

        data = self.db.credentials()
        if not data:
            raise DriveError("Drive access is not connected. Sign in again.")
        self.creds = Credentials.from_authorized_user_info(data, scopes=[DRIVE_READONLY_SCOPE])
        return self.creds

    def headers(self) -> dict[str, str]:
        from google.auth.transport.requests import Request as GoogleRequest

        with self.db.auth_lock, self._lock:
            if self.generation != self.db.auth_generation():
                raise DriveError("Drive session ended. Sign in again.")
            creds = self.creds or self._load()
            if not creds.valid:
                creds.refresh(GoogleRequest())
                if not self.db.save_credentials(creds.to_json(), self.generation):
                    raise DriveError("Drive session ended during refresh")
            self.db.sync_credentials(self._credentials_path)
            return {"Authorization": f"Bearer {creds.token}"}

    def refresh(self) -> None:
        with self.db.auth_lock, self._lock:
            self.creds = None
            self.headers()


def _range_header(value: str, size: int) -> tuple[int, int]:
    match = re.fullmatch(r"bytes=(\d+)-(\d*)", value.strip())
    if match and size > 0:
        start = int(match[1])
        end = min(int(match[2]) if match[2] else size - 1, size - 1)
        if start < size and end >= start:
            return start, end
    suffix = re.fullmatch(r"bytes=-(\d+)", value.strip())
    if suffix and size > 0 and int(suffix[1]) > 0:
        return max(0, size - int(suffix[1])), size - 1
    raise ValueError("invalid or unsatisfiable range")


def _mime_type(track: dict[str, Any]) -> str:
    provided = track.get("mime_type")
    if isinstance(provided, str) and provided.startswith("audio/"):
        return provided
    return {
        ".m4a": "audio/mp4",
        ".m4b": "audio/mp4",
        ".mp3": "audio/mpeg",
        ".flac": "audio/flac",
        ".ogg": "audio/ogg",
        ".opus": "audio/ogg",
        ".wav": "audio/wav",
        ".aac": "audio/aac",
    }.get(Path(track["name"]).suffix.lower(), "application/octet-stream")


class LibraryScanner:
    """Background Drive scanner with per-file checkpoints and retry support."""

    def __init__(self, config: WebConfig, db: Database) -> None:
        self.config = config
        self.db = db
        self.lock = threading.Lock()
        self.events: list[dict[str, Any]] = []
        self.event_id = 0
        self.event_lock = threading.Lock()

    def publish(self, **event: Any) -> None:
        with self.event_lock:
            self.event_id += 1
            self.events.append({"id": self.event_id, **event})
            self.events = self.events[-200:]
        self.db.save_scan_status(**event)

    def start(self, retry_failed: bool = True) -> bool:
        if not self.lock.acquire(blocking=False):
            return False
        try:
            old_status = self.db.scan_status() or {}
            resume = old_status.get("status") in {"running", "failed", "interrupted"}
            self.db.save_scan_status(status="running", error="", current="")
            threading.Thread(target=self.run, args=(retry_failed, resume), daemon=True).start()
            return True
        except BaseException:
            self.lock.release()
            raise

    def run(self, retry_failed: bool = True, resume: bool = False) -> None:
        try:
            self._run(retry_failed, resume)
        except Exception as exc:
            logger.exception("Drive library scan failed")
            self.publish(status="failed", error=str(exc), current="")
        finally:
            self.lock.release()

    def stop_incomplete_scan(self) -> None:
        status = self.db.scan_status()
        if status and status.get("status") == "running":
            self.db.save_scan_status(status="interrupted", current="")

    def _run(self, retry_failed: bool, resume: bool) -> None:
        self.config.check()
        auth = StoredDriveAuth(self.db, self.config.credentials_path)
        source = DriveSource(
            folder_id=self.config.folder_id,
            auth=auth,
            timeout=60,
            budget_bytes=None,
            max_retries=3,
        )
        try:
            self.publish(
                status="running",
                total=0,
                processed=0,
                current="Connecting to Google Drive",
                error="",
            )
            items = self.db.pending_scan_items() if resume else []
            if not resume:
                self.db.clear_scan_items()
                self.db.clear_scan_directories()
                items = []
            elif self.db.scan_file_count() == 0:
                self.db.reset_scan_inventory()
            failed_ids = {item["id"] for item in items if item.get("error")}
            excluded_directories = self.db.excluded_directories()
            excluded = {item["id"] for item in excluded_directories}
            excluded_prefixes = [item["path"].rstrip("/") + "/" for item in excluded_directories]
            if retry_failed:
                with self.db.connect() as connection:
                    connection.execute("DELETE FROM scan_items WHERE error != ''")
            items = [
                item
                for item in self.db.pending_scan_items()
                if not any(item.get("path", "").startswith(path) for path in excluded_prefixes)
            ]
            seen = {item["id"] for item in items if not retry_failed or not item.get("error")}
            for item in self.db.pending_scan_items():
                if any(item.get("path", "").startswith(path) for path in excluded_prefixes):
                    with self.db.connect() as connection:
                        connection.execute("DELETE FROM scan_items WHERE id=?", (item["id"],))
            failures = 0
            total = len(seen)
            processed = len(seen)
            if not self.db.inventory_directories():
                self.db.enqueue_scan_directory(self.config.folder_id, "")
            # Inventory every folder first. Persist children before marking a parent complete.
            while directories := self.db.inventory_directories(listed=False):
                folder_id, prefix, _listed, _scanned, _files_listed = directories[0]
                if folder_id in excluded or any(
                    prefix.startswith(path) for path in excluded_prefixes
                ):
                    self.db.mark_scan_directory(folder_id, listed=True)
                    continue
                self.publish(
                    status="running",
                    total=len(seen),
                    processed=len(seen),
                    current=f"Listing folders: {prefix or '/'}",
                    error="",
                )
                folders = list(source.iter_child_directories(folder_id, prefix))
                self.db.queue_scan_directories(
                    [
                        folder
                        for folder in folders
                        if folder[0] not in excluded
                        and not any(folder[1].startswith(path) for path in excluded_prefixes)
                    ]
                )
                self.db.mark_scan_directory(folder_id, listed=True)

            total = len(seen)
            processed = len(seen)
            failures = 0
            for (
                folder_id,
                prefix,
                _listed,
                scanned,
                files_listed,
            ) in self.db.inventory_directories():
                if folder_id in excluded or any(
                    prefix.startswith(path) for path in excluded_prefixes
                ):
                    self.db.mark_scan_directory(folder_id, scanned=True)
                    continue
                if scanned and not self.db.scan_files_for_directory(folder_id, seen):
                    continue
                self.publish(
                    status="running",
                    total=total,
                    processed=processed,
                    current=f"Listing files: {prefix or '/'}",
                    error="",
                )
                if not files_listed:
                    files = list(source.iter_directory_files(folder_id, prefix))
                    self.db.save_scan_files(folder_id, files)
                pending = self.db.scan_files_for_directory(folder_id, seen)
                total = self.db.scan_file_count()
                self.publish(total=total, processed=processed, current=prefix or "/")
                with ThreadPoolExecutor(max_workers=5, thread_name_prefix="drive-probe") as pool:
                    futures = {
                        pool.submit(self._probe_remote, source, remote): remote
                        for remote in pending
                    }
                    for future in as_completed(futures):
                        remote = futures[future]
                        item = future.result()
                        error = item["error"]
                        logger.info(
                            "Drive scan file complete path=%s error=%s", remote.path, bool(error)
                        )
                        self.db.save_scan_item(item, error)
                        items.append(item)
                        seen.add(remote.id)
                        processed += 1
                        failures += bool(error)
                        self.publish(
                            status="running",
                            total=total,
                            processed=processed,
                            current=remote.path,
                            error="",
                        )
                        if error:
                            failed_ids.add(remote.id)
                self.db.mark_scan_directory(folder_id, scanned=True)
            with self.db.auth_lock:
                if auth.generation != self.db.auth_generation():
                    raise DriveError("Drive session ended during scan")
                self._commit(items)
            remaining_failures = len(failed_ids) if not retry_failed else failures
            current_status = self.db.scan_status()
            if current_status and current_status.get("status") == "failed":
                self.db.save_scan_status(processed=processed - failures)
            self.publish(
                status="completed",
                total=total,
                processed=processed,
                current="",
                error=(
                    f"{remaining_failures} file(s) failed after retries"
                    if remaining_failures
                    else ""
                ),
            )
        finally:
            source.close()

    def _probe_remote(self, source: DriveSource, remote: RemoteFile) -> dict[str, Any]:
        track: TrackMeta | None = None
        error = ""
        for attempt in range(3):
            logger.info("Drive scan probing file attempt=%d path=%s", attempt + 1, remote.path)
            stream = None
            try:
                stream = source.open(remote)
                track = probe(
                    stream,
                    name=remote.name,
                    path=remote.path,
                    source=SOURCE_GDRIVE,
                    file_id=remote.id,
                    size=remote.size,
                    mime_type=remote.mime_type,
                    modified=remote.modified,
                )
                error = track.error or ""
                if track.retryable_error and attempt < 2:
                    time.sleep(0.5 * (2**attempt))
                    continue
                break
            except Exception as exc:
                error = str(exc)
                if attempt < 2:
                    time.sleep(0.5 * (2**attempt))
            finally:
                if stream is not None:
                    stream.close()
        if track is None:
            track = TrackMeta(
                source=SOURCE_GDRIVE,
                id=remote.id,
                name=remote.name,
                path=remote.path,
                size=remote.size,
                mime_type=remote.mime_type,
                error=error,
            )
        return {
            "id": remote.id,
            "path": remote.path,
            "name": remote.name,
            "size": remote.size,
            "mime_type": remote.mime_type,
            "modified": remote.modified,
            "md5": remote.md5,
            "meta": track.to_dict(),
            "error": error,
        }

    def _commit(self, items: list[dict[str, Any]]) -> None:
        # A temporarily unreadable existing track keeps its last successful metadata.
        previous = self.db.identities()
        for item in items:
            if item.get("error") and item["id"] in previous:
                old = self.db.track(item["id"], include_chapters=True)
                if old:
                    item = dict(item)
                    item["meta"] = old.get("probe_meta", {**item["meta"], **old})
                    items = [item if entry["id"] == item["id"] else entry for entry in items]
        identities = self.db.identities()
        used_ids: set[str] = set()
        metadata = []
        for item in items:
            values = {
                key: value
                for key, value in item["meta"].items()
                if key in TrackMeta.__dataclass_fields__
            }
            values["covers"] = [
                Cover(
                    **{
                        key: value
                        for key, value in cover.items()
                        if key in Cover.__dataclass_fields__
                    }
                )
                for cover in values.get("covers", [])
            ]
            values["chapters"] = [
                Chapter(
                    **{
                        key: value
                        for key, value in chapter.items()
                        if key in Chapter.__dataclass_fields__
                    }
                )
                for chapter in values.get("chapters", [])
            ]
            metadata.append(TrackMeta(**values))
        remote_by_id = {item["id"]: item for item in items}
        books: list[dict[str, Any]] = []
        tracks: list[dict[str, Any]] = []
        for group in group_tracks(metadata):
            candidates = sorted(
                {identities[t.track.id] for t in group.tracks if t.track.id in identities}
            )
            book_id = next(
                (candidate for candidate in candidates if candidate not in used_ids), None
            )
            if book_id is None:
                book_id = "book-" + secrets.token_hex(16)
            used_ids.add(book_id)
            members = []
            for grouped in group.tracks:
                meta = grouped.track.to_dict()
                original = remote_by_id[meta["id"]]
                track = {
                    "id": original["id"],
                    "book_id": book_id,
                    "name": original["name"],
                    "path": original["path"],
                    "size": original["size"] or 0,
                    "md5": original.get("md5"),
                    "modified": original.get("modified"),
                    "mime_type": original["mime_type"],
                    "album": meta.get("album"),
                    "title": meta.get("title") or original["name"],
                    "duration": meta.get("duration") or 0,
                    "chapters": meta.get("chapters") or [],
                    "chapter_count": len(meta.get("chapters") or []),
                    "format": meta.get("format"),
                    "probe_meta": meta,
                }
                tracks.append(track)
                members.append({key: value for key, value in track.items() if key != "probe_meta"})
            first = group.tracks[0].track
            books.append(
                {
                    "id": book_id,
                    "title": group.title,
                    "artist": first.artist,
                    "album_artist": first.albumartist,
                    "album": first.album or group.title,
                    "directory": members[0]["path"].rsplit("/", 1)[0]
                    if "/" in members[0]["path"]
                    else "",
                    "cover": f"/api/tracks/{members[0]['id']}/cover" if first.covers else None,
                    "duration": group.total_duration,
                    "tracks": members,
                }
            )
        self.db.save_library(books, tracks)
        logger.info("Drive scan published books=%d tracks=%d", len(books), len(tracks))


def create_app(config: WebConfig | None = None, db: Database | None = None) -> FastAPI:
    config = config or WebConfig()
    db = db or Database(config.db_path)
    cache = MediaCache(config.cache_path, config.cache_max_bytes)
    cache_warm_slots = threading.BoundedSemaphore(2)

    def warm_track(track: dict[str, Any]) -> None:
        client = httpx.Client(follow_redirects=True)
        try:
            if cache.file(track) is not None:
                return
            auth = StoredDriveAuth(db, config.credentials_path)
            url = f"{DRIVE_API}/{track['id']}?alt=media&supportsAllDrives=true"
            fetcher = HttpRangeFetcher.open(
                url,
                size=int(track["size"]),
                client=client,
                auth=auth,
                name=track["name"],
            )
            reader = SeekableBlockReader(fetcher, block_size=256 * 1024, name=track["name"])
            try:

                def chunks() -> Iterator[bytes]:
                    while chunk := reader.read(256 * 1024):
                        yield chunk

                cache.put(track, chunks())
            finally:
                reader.close()
        except Exception:
            logger.info("Media warm failed track=%s", track.get("id"), exc_info=True)
        finally:
            client.close()
            cache_warm_slots.release()

    def schedule_warm(track: dict[str, Any]) -> bool:
        if not config.cache_warm_enabled or cache.file(track) is not None:
            return True
        if not cache_warm_slots.acquire(blocking=False):
            return False
        threading.Thread(
            target=warm_track,
            args=(track,),
            name="media-cache-warm",
            daemon=True,
        ).start()
        return True

    try:
        cache._evict(Path())
    except OSError:
        logger.info("Media cache startup eviction failed", exc_info=True)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        config.check()
        yield

    app = FastAPI(title="RuangDengar", version="1.0", lifespan=lifespan)

    @app.exception_handler(StarletteHTTPException)
    async def api_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail
        codes = {
            401: "unauthorized",
            403: "forbidden",
            404: "not_found",
            405: "method_not_allowed",
            409: "conflict",
            428: "precondition_required",
            503: "service_unavailable",
            422: "invalid_request",
            502: "upstream_error",
        }
        code = (
            detail.get("code", "conflict")
            if isinstance(detail, dict)
            else codes.get(exc.status_code, "request_failed")
        )
        return JSONResponse(
            {"detail": detail, "code": code}, status_code=exc.status_code, headers=exc.headers
        )

    app.add_middleware(
        SessionMiddleware,
        secret_key=config.secret or secrets.token_urlsafe(48),
        https_only=config.cookie_secure,
        same_site="lax",
    )
    app.state.config = config
    app.state.db = db
    scanner = LibraryScanner(config, db)
    app.state.scanner = scanner

    @app.middleware("http")
    async def recover_interrupted_scan(request: Request, call_next: Any) -> Response:
        if not getattr(app.state, "scan_recovered", False):
            app.state.scan_recovered = True
            scanner.stop_incomplete_scan()
            status = db.scan_status()
            if status and status.get("status") == "interrupted":
                scanner.start()
        response = await call_next(request)
        if request.url.path in {"/auth/google", "/auth/callback", "/", "/api/library"}:
            set_cookie = response.headers.get("set-cookie", "")
            logger.warning(
                "auth_http method=%s path=%s status=%s scheme=%s host=%s cookie_in=%s "
                "session_email=%s set_cookie=%s set_cookie_secure=%s",
                request.method,
                request.url.path,
                response.status_code,
                request.url.scheme,
                request.url.hostname,
                bool(request.cookies.get("session")),
                bool(request.session.get("email")),
                bool(set_cookie),
                "secure" in set_cookie.lower(),
            )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "private, no-store"
        return response

    def user(request: Request) -> str:
        email = str(request.session.get("email", "")).lower()
        if (
            not email
            or email != config.allowed_email
            or request.session.get("generation") != db.auth_generation()
        ):
            logger.warning(
                "auth_rejected path=%s cookie_in=%s session_email=%s allowed_email_configured=%s",
                request.url.path,
                bool(request.cookies.get("session")),
                bool(email),
                bool(config.allowed_email),
            )
            raise HTTPException(401, "Sign in required")
        return email

    def flow_for(state: str | None = None, code_verifier: str | None = None) -> Any:
        from google_auth_oauthlib.flow import Flow

        if not config.client_secrets.is_file():
            raise HTTPException(503, "Set GOOGLE_CLIENT_SECRETS to an OAuth web client JSON file")
        if code_verifier is None and state is None:
            code_verifier = secrets.token_urlsafe(64)
        flow = Flow.from_client_secrets_file(
            str(config.client_secrets),
            scopes=["openid", "email", "profile", DRIVE_READONLY_SCOPE],
            state=state,
            code_verifier=code_verifier,
            autogenerate_code_verifier=False,
        )
        if code_verifier is not None:
            flow.oauth2session._client.code_verifier = code_verifier
        flow.redirect_uri = f"{config.public_base_url}/auth/callback"
        return flow

    @app.get("/auth/google")
    def auth_google(request: Request) -> RedirectResponse:
        if not config.allowed_email:
            raise HTTPException(503, "Set APP_ALLOWED_EMAIL before enabling sign-in")
        state = secrets.token_urlsafe(32)
        flow = flow_for()
        url, _ = flow.authorization_url(
            access_type="offline", include_granted_scopes="true", prompt="consent", state=state
        )
        request.session["oauth_state"] = state
        request.session["oauth_callback_url"] = flow.redirect_uri
        request.session["oauth_code_verifier"] = flow.code_verifier
        return RedirectResponse(url)

    @app.get("/auth/callback")
    def auth_callback(
        request: Request, state: str = "", code: str = "", error: str = ""
    ) -> RedirectResponse:
        expected = request.session.pop("oauth_state", "")
        valid_state = state and expected and secrets.compare_digest(state, expected)
        if error or not code or not valid_state:
            raise HTTPException(400, "Google sign-in was cancelled or state validation failed")
        flow = flow_for(state, request.session.pop("oauth_code_verifier", None))
        if request.session.pop("oauth_callback_url", "") != flow.redirect_uri:
            raise HTTPException(400, "OAuth callback URL changed during sign-in")
        if not flow.code_verifier:
            raise HTTPException(400, "OAuth code verifier missing. Start sign-in again.")
        try:
            from oauthlib.oauth2 import WebApplicationClient

            os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
            client = WebApplicationClient(flow.client_config["client_id"])
            client.code_verifier = flow.code_verifier
            flow.oauth2session._client = client
            flow.fetch_token(code=code)
        except Exception as exc:
            logger.exception("Google OAuth token exchange failed")
            raise HTTPException(
                400, "Google token exchange failed. Check server logs, then start sign-in again."
            ) from exc
        creds = flow.credentials
        try:
            from google.auth.transport.requests import Request as GoogleRequest
            from google.oauth2 import id_token

            if not creds.id_token:
                raise ValueError("Google did not return an identity token")
            identity = id_token.verify_oauth2_token(
                creds.id_token, GoogleRequest(), audience=flow.client_config["client_id"]
            )
        except Exception as exc:
            raise HTTPException(401, "Google identity token validation failed") from exc
        email = str(identity.get("email", "")).lower()
        if not identity.get("email_verified") or email != config.allowed_email:
            raise HTTPException(403, "This Google account is not allowed")
        granted = set(creds.scopes or [])
        if DRIVE_READONLY_SCOPE not in granted:
            raise HTTPException(403, "Approve read-only Google Drive access to use the library")
        if not creds.refresh_token:
            raise HTTPException(403, "Reconnect Google with offline access enabled")
        generation = db.connect_account(creds.to_json(), config.credentials_path)
        request.session.clear()
        request.session["email"] = email
        request.session["generation"] = generation
        return RedirectResponse("/", headers={"Cache-Control": "no-store"})

    @app.post("/auth/logout")
    def logout(request: Request) -> Response:
        user(request)
        request.session.clear()
        db.disconnect(config.credentials_path)
        return Response(status_code=204)

    @app.get("/api/v1/books", response_model=BookPage)
    def paged_books(
        request: Request,
        q: str = Query("", max_length=200),
        sort: str = "title",
        direction: str = "asc",
        status: str = "all",
        favorite: bool = False,
        playlist_id: str | None = None,
        limit: int = Query(50, ge=1, le=100),
        cursor: str | None = Query(None, max_length=2048),
    ) -> dict[str, Any]:
        user(request)
        return db.catalog_page(
            q=q,
            sort=sort,
            direction=direction,
            status=status,
            favorite=favorite,
            playlist_id=playlist_id,
            limit=limit,
            cursor=cursor,
        )

    @app.get("/api/v1/books/{book_id}/playback", response_model=PlaybackResponse)
    def playback(book_id: str, request: Request) -> dict[str, Any]:
        user(request)
        book = db.book(book_id, include_chapters=False)
        if book is None:
            raise HTTPException(404, "Book not found")
        return {
            "book_id": book_id,
            "tracks": book["tracks"],
            "progress": book["progress"],
            "revision": db.revision(),
        }

    @app.get("/api/v1/history", response_model=HistoryPage)
    def history_page(
        request: Request,
        limit: int = Query(50, ge=1, le=100),
        cursor: int | None = Query(None, ge=1),
    ) -> dict[str, Any]:
        user(request)
        with db.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM listening_history WHERE id<? ORDER BY id DESC LIMIT ?",
                (cursor or 9223372036854775807, limit + 1),
            ).fetchall()
        return {
            "items": [dict(row) for row in rows[:limit]],
            "next_cursor": rows[limit - 1]["id"] if len(rows) > limit else None,
        }

    @app.get("/api/bootstrap", response_model=BootstrapResponse)
    def bootstrap(request: Request) -> dict[str, Any]:
        user(request)
        db.sync_credentials(config.credentials_path)
        # One database snapshot for catalog, personal state, totals, and revision.
        with db.connect() as connection:
            connection.execute("BEGIN")
            state = connection.execute("SELECT * FROM scan_state WHERE id=1").fetchone()
            return {
                "books": db.library(connection),
                "features": db.feature_data(connection),
                "storage": storage_info(connection),
                "scan": dict(state) if state else {"status": "idle"},
                "revision": connection.execute(
                    "SELECT revision FROM catalog_state WHERE id=1"
                ).fetchone()[0],
            }

    @app.get("/api/v1/bootstrap", response_model=BoundedBootstrapResponse)
    def bounded_bootstrap(request: Request) -> dict[str, Any]:
        user(request)
        return {
            "books": db.catalog_page(limit=20),
            "scan": db.scan_status() or {"status": "idle"},
            "capabilities": {"conditional_progress": True, "playlist_items": True},
        }

    @app.get("/api/library", response_model=list[BookResponse])
    def library(request: Request, refresh: bool = False) -> list[dict[str, Any]]:
        user(request)
        try:
            config.check()
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc
        books = db.library()
        if refresh:
            raise HTTPException(405, "Use POST /api/v1/scan-jobs to scan the library")
        return books

    @app.post("/api/v1/scan-jobs", status_code=202, response_model=ScanResponse)
    @app.post("/api/library/refresh", response_model=ScanResponse)
    def library_refresh(request: Request, retry_failed: bool = True) -> dict[str, Any]:
        user(request)
        if not scanner.start(retry_failed=retry_failed):
            raise HTTPException(409, "Library scan is already running")
        return db.scan_status() or {"status": "running"}

    @app.get("/api/v1/scan-jobs/current", response_model=ScanResponse)
    @app.get("/api/library/scan", response_model=ScanResponse)
    def library_scan(request: Request) -> dict[str, Any]:
        user(request)
        return scanner.db.scan_status() or {
            "status": "idle",
            "total": 0,
            "processed": 0,
            "current": "",
            "error": "",
        }

    @app.get("/api/v1/scan-jobs/current/events")
    @app.get("/api/library/events")
    def library_events(request: Request) -> StreamingResponse:
        user(request)

        async def stream_events() -> Any:
            # Start at the current event cursor; old completed scans must not
            # terminate a subscription opened for a newer scan.
            with scanner.event_lock:
                last_id = scanner.event_id
            initial = await run_in_threadpool(scanner.db.scan_status) or {"status": "idle"}
            yield f"data: {json.dumps(initial)}\n\n"
            while not await request.is_disconnected():
                with scanner.event_lock:
                    pending = [event for event in scanner.events if event["id"] > last_id]
                if pending:
                    for event in pending:
                        last_id = event["id"]
                        yield f"id: {last_id}\ndata: {json.dumps(event)}\n\n"
                    if pending[-1].get("status") in {"completed", "failed"}:
                        return
                else:
                    status = await run_in_threadpool(scanner.db.scan_status) or {"status": "idle"}
                    yield f"data: {json.dumps(status)}\n\n"
                    if status.get("status") != "running":
                        return
                await asyncio.sleep(1)

        return StreamingResponse(stream_events(), media_type="text/event-stream")

    @app.get("/api/v1/books/{book_id}", response_model=BookResponse)
    @app.get("/api/books/{book_id}", response_model=BookResponse)
    def get_book(book_id: str, request: Request) -> dict[str, Any]:
        user(request)
        book = db.book(book_id, include_chapters=False)
        if book is None:
            raise HTTPException(404, "Book not found")
        if book.get("tracks"):
            for track in book["tracks"]:
                track["chapter_count"] = track.get("chapter_count", len(track.get("chapters", [])))
                track["chapters"] = []
            return book
        raise HTTPException(404, "Book has no playable tracks")

    @app.post("/api/books/{book_id}/metadata/search")
    async def search_book_metadata(book_id: str, request: Request) -> dict[str, Any]:
        await run_in_threadpool(user, request)
        book = await run_in_threadpool(db.book, book_id)
        if book is None:
            raise HTTPException(404, "Book not found")
        params = {
            "q": f"{book.get('title', '')} {book.get('artist', '')}".strip(),
            "maxResults": "8",
        }
        candidates = []
        source_errors = []
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
                response = await client.get(
                    "https://www.googleapis.com/books/v1/volumes", params=params
                )
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError):
            source_errors.append("Google Books")
            payload = {}
        for item in payload.get("items", []):
            info = item.get("volumeInfo", {})
            isbn = next(
                (
                    entry["identifier"]
                    for entry in info.get("industryIdentifiers", [])
                    if entry.get("type") in {"ISBN_13", "ISBN_10"}
                ),
                None,
            )
            cover = info.get("imageLinks", {}).get("thumbnail") or ""
            candidate = {
                "source": "Google Books",
                "source_id": item.get("id"),
                "title": info.get("title"),
                "authors": info.get("authors", []),
                "description": info.get("description"),
                "publisher": info.get("publisher"),
                "published_date": info.get("publishedDate"),
                "isbn": isbn,
                "cover": cover.replace("http://", "https://"),
            }
            if candidate["title"] and candidate["source_id"]:
                candidates.append(candidate)
        open_library_params = {
            "title": str(book.get("title", "")),
            "author": str(book.get("artist", "")),
            "fields": "key,title,author_name,first_publish_year,cover_i",
            "limit": "8",
        }
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
                response = await client.get(
                    "https://openlibrary.org/search.json", params=open_library_params
                )
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError):
            source_errors.append("Open Library")
            payload = {}
        for item in payload.get("docs", []):
            cover_id = item.get("cover_i")
            if item.get("title") and re.fullmatch(r"/works/OL[0-9]+W", str(item.get("key", ""))):
                candidates.append(
                    {
                        "source": "Open Library",
                        "source_id": str(item["key"]).removeprefix("/works/"),
                        "title": item.get("title"),
                        "authors": item.get("author_name", []),
                        "published_date": str(item.get("first_publish_year", "")),
                        "cover": (
                            f"https://covers.openlibrary.org/b/id/{cover_id}-M.jpg"
                            if cover_id
                            else ""
                        ),
                    }
                )
        if not candidates and source_errors:
            raise HTTPException(
                502,
                "Metadata search failed for: " + ", ".join(source_errors),
            )
        return {"candidates": candidates, "source_errors": source_errors}

    @app.post("/api/books/{book_id}/metadata/apply", response_model=BookResponse)
    async def apply_book_metadata(
        book_id: str, request: Request, input_data: MetadataBody
    ) -> dict[str, Any]:
        await run_in_threadpool(user, request)
        if await run_in_threadpool(db.book, book_id) is None:
            raise HTTPException(404, "Book not found")
        body = input_data.model_dump()
        if (
            not isinstance(body, dict)
            or body.get("source") not in {"Google Books", "Open Library"}
            or not isinstance(body.get("source_id"), str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", body["source_id"])
        ):
            raise HTTPException(422, "metadata source or source_id is invalid")
        if body["source"] == "Google Books":
            url = f"https://www.googleapis.com/books/v1/volumes/{body['source_id']}"
        else:
            url = f"https://openlibrary.org/works/{body['source_id']}.json"
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
                response = await client.get(url)
                response.raise_for_status()
                payload = response.json()
                if body["source"] == "Google Books":
                    info = payload.get("volumeInfo", {})
                else:
                    authors = []
                    for entry in payload.get("authors", [])[:10]:
                        key = entry.get("author", {}).get("key", "")
                        if re.fullmatch(r"/authors/OL[0-9]+A", key):
                            author_response = await client.get(f"https://openlibrary.org{key}.json")
                            author_response.raise_for_status()
                            name = author_response.json().get("name")
                            if isinstance(name, str):
                                authors.append(name)
                    info = {
                        "title": payload.get("title"),
                        "description": payload.get("description"),
                        "authors": authors,
                        "covers": payload.get("covers", []),
                        "publishedDate": payload.get("first_publish_date", ""),
                    }
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(502, "Could not load selected metadata") from exc
        identifiers = info.get("industryIdentifiers", [])
        isbn = next(
            (
                entry["identifier"]
                for entry in identifiers
                if entry.get("type") in {"ISBN_13", "ISBN_10"}
            ),
            None,
        )
        image_links = info.get("imageLinks", {})
        cover = image_links.get("thumbnail") or ""
        if body["source"] == "Open Library" and info.get("covers"):
            cover = f"https://covers.openlibrary.org/b/id/{info['covers'][0]}-L.jpg"
        description = info.get("description") or ""
        if isinstance(description, dict):
            description = description.get("value", "")
        metadata = {
            "metadata_source": {"source": body["source"], "source_id": body["source_id"]},
            "title": info.get("title"),
            "artist": ", ".join(info.get("authors", [])) if info.get("authors") else None,
            "description": str(description),
            "publisher": info.get("publisher", ""),
            "published_date": info.get("publishedDate", ""),
            "isbn": isbn,
            "cover": cover.replace("http://", "https://") or None,
        }
        updated = await run_in_threadpool(
            db.update_book_metadata,
            book_id,
            {key: value for key, value in metadata.items() if value},
        )
        if updated is None:
            raise HTTPException(404, "Book not found")
        return updated

    @app.put("/api/v1/books/{book_id}/progress", response_model=ProgressResponse)
    def conditional_progress(
        book_id: str, request: Request, payload: ConditionalProgressBody
    ) -> dict[str, Any]:
        user(request)
        return db.checkpoint(book_id, **payload.model_dump())

    @app.put("/api/progress/{book_id}", response_model=ProgressResponse)
    def progress(book_id: str, request: Request, payload: ProgressBody) -> dict[str, Any]:
        user(request)
        if payload.base_revision is None or payload.event_id is None:
            raise HTTPException(428, "base_revision and event_id are required; read progress first")
        return db.checkpoint(book_id, **payload.model_dump())

    @app.get("/api/features", response_model=FeaturesResponse)
    def features(request: Request) -> dict[str, Any]:
        user(request)
        return db.feature_data()

    @app.put("/api/books/{book_id}/favorite")
    def favorite(book_id: str, request: Request, payload: FavoriteBody) -> dict[str, bool]:
        user(request)
        if db.book(book_id) is None:
            raise HTTPException(404, "Book not found")
        body = payload.model_dump()
        if not isinstance(body, dict) or not isinstance(body.get("favorite"), bool):
            raise HTTPException(422, "favorite must be a boolean")
        with db.book_write(book_id) as connection:
            if body["favorite"]:
                connection.execute("INSERT OR IGNORE INTO favorites(book_id) VALUES(?)", (book_id,))
            else:
                connection.execute("DELETE FROM favorites WHERE book_id=?", (book_id,))
            bump_revision(connection)
        return {"favorite": body["favorite"]}

    @app.put("/api/books/{book_id}/rating")
    def rating(book_id: str, request: Request, payload: RatingBody) -> dict[str, int | None]:
        user(request)
        if db.book(book_id) is None:
            raise HTTPException(404, "Book not found")
        body = payload.model_dump()
        value = body.get("rating") if isinstance(body, dict) else None
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 5
        ):
            raise HTTPException(422, "rating must be an integer from 1 to 5, or null")
        with db.book_write(book_id) as connection:
            if value is None:
                connection.execute("DELETE FROM ratings WHERE book_id=?", (book_id,))
            else:
                connection.execute(
                    "INSERT INTO ratings(book_id,rating) VALUES(?,?) "
                    "ON CONFLICT(book_id) DO UPDATE SET rating=excluded.rating",
                    (book_id, value),
                )
            bump_revision(connection)
        return {"rating": value}

    @app.put("/api/books/{book_id}/tags")
    def tags(book_id: str, request: Request, payload: TagsBody) -> dict[str, list[str]]:
        user(request)
        if db.book(book_id) is None:
            raise HTTPException(404, "Book not found")
        body = payload.model_dump()
        values = body.get("tags") if isinstance(body, dict) else None
        if (
            not isinstance(values, list)
            or len(values) > 20
            or any(
                not isinstance(tag, str) or not tag.strip() or len(tag.strip()) > 40
                for tag in values
            )
        ):
            raise HTTPException(
                422, "tags must be a list of up to 20 non-empty strings of at most 40 characters"
            )
        clean = list(dict.fromkeys(tag.strip() for tag in values))
        with db.book_write(book_id) as connection:
            connection.execute("DELETE FROM book_tags WHERE book_id=?", (book_id,))
            connection.executemany(
                "INSERT INTO book_tags(book_id,tag) VALUES(?,?)", [(book_id, tag) for tag in clean]
            )
            bump_revision(connection)
        return {"tags": clean}

    @app.post("/api/playlists", status_code=201, response_model=PlaylistResponse)
    def create_playlist(request: Request, payload: PlaylistBody) -> dict[str, Any]:
        user(request)
        body = payload.model_dump()
        name = (
            body.get("name", "").strip()
            if isinstance(body, dict) and isinstance(body.get("name"), str)
            else ""
        )
        if not name or len(name) > 80:
            raise HTTPException(422, "playlist name must be 1..80 characters")
        playlist_id = secrets.token_urlsafe(12)
        with db.connect() as connection:
            connection.execute("INSERT INTO playlists(id,name) VALUES(?,?)", (playlist_id, name))
        return {"id": playlist_id, "name": name, "book_ids": [], "revision": 0}

    @app.put("/api/v1/playlists/{playlist_id}/books", response_model=PlaylistBooksResponse)
    @app.put("/api/playlists/{playlist_id}/books", response_model=PlaylistBooksResponse)
    def playlist_books(
        playlist_id: str, request: Request, payload: PlaylistBooksBody
    ) -> dict[str, Any]:
        user(request)
        body = payload.model_dump()
        ids = body.get("book_ids") if isinstance(body, dict) else None
        if (
            not isinstance(ids, list)
            or len(ids) > 1000
            or any(not isinstance(item, str) for item in ids)
            or len(set(ids)) != len(ids)
        ):
            raise HTTPException(422, "book_ids must be a unique list of at most 1000 IDs")
        with db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = {row[0] for row in connection.execute("SELECT id FROM books")}
            if any(item not in current for item in ids):
                raise HTTPException(422, "all playlist books must exist in the library")
            if (
                connection.execute("SELECT 1 FROM playlists WHERE id=?", (playlist_id,)).fetchone()
                is None
            ):
                raise HTTPException(404, "Playlist not found")
            revision = connection.execute(
                "SELECT revision FROM playlists WHERE id=?", (playlist_id,)
            ).fetchone()[0]
            if payload.base_revision is None:
                raise HTTPException(428, "base_revision is required for playlist replacement")
            if payload.base_revision != revision:
                raise HTTPException(409, "Playlist changed; reload before reordering")
            connection.execute("DELETE FROM playlist_books WHERE playlist_id=?", (playlist_id,))
            connection.executemany(
                "INSERT INTO playlist_books(playlist_id,book_id,position) VALUES(?,?,?)",
                [(playlist_id, book_id, i) for i, book_id in enumerate(ids)],
            )
            connection.execute(
                "UPDATE playlists SET revision=revision+1 WHERE id=?", (playlist_id,)
            )
            bump_revision(connection)
        return {"book_ids": ids, "revision": revision + 1}

    @app.put("/api/v1/playlists/{playlist_id}/books/{book_id}", response_model=PlaylistResponse)
    @app.put("/api/playlists/{playlist_id}/books/{book_id}", response_model=PlaylistResponse)
    def add_playlist_book(playlist_id: str, book_id: str, request: Request) -> dict[str, Any]:
        user(request)
        return change_playlist_item(playlist_id, book_id, True)

    @app.delete("/api/v1/playlists/{playlist_id}/books/{book_id}", response_model=PlaylistResponse)
    @app.delete("/api/playlists/{playlist_id}/books/{book_id}", response_model=PlaylistResponse)
    def remove_playlist_book(playlist_id: str, book_id: str, request: Request) -> dict[str, Any]:
        user(request)
        return change_playlist_item(playlist_id, book_id, False)

    def change_playlist_item(playlist_id: str, book_id: str, add: bool) -> dict[str, Any]:
        with db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            playlist = connection.execute(
                "SELECT * FROM playlists WHERE id=?", (playlist_id,)
            ).fetchone()
            if playlist is None:
                raise HTTPException(404, "Playlist not found")
            if add:
                if not connection.execute("SELECT 1 FROM books WHERE id=?", (book_id,)).fetchone():
                    raise HTTPException(404, "Book not found")
                count = connection.execute(
                    "SELECT COUNT(*) FROM playlist_books WHERE playlist_id=?", (playlist_id,)
                ).fetchone()[0]
                exists = connection.execute(
                    "SELECT 1 FROM playlist_books WHERE playlist_id=? AND book_id=?",
                    (playlist_id, book_id),
                ).fetchone()
                if count >= 1000 and not exists:
                    raise HTTPException(422, "Playlist is full")
                changed = connection.execute(
                    "INSERT OR IGNORE INTO playlist_books "
                    "SELECT ?,?,COALESCE(MAX(position),-1)+1 FROM playlist_books "
                    "WHERE playlist_id=?",
                    (playlist_id, book_id, playlist_id),
                ).rowcount
            else:
                changed = connection.execute(
                    "DELETE FROM playlist_books WHERE playlist_id=? AND book_id=?",
                    (playlist_id, book_id),
                ).rowcount
            if changed:
                connection.execute(
                    "UPDATE playlists SET revision=revision+1 WHERE id=?", (playlist_id,)
                )
                bump_revision(connection)
            result = dict(
                connection.execute("SELECT * FROM playlists WHERE id=?", (playlist_id,)).fetchone()
            )
            result["book_ids"] = [
                row[0]
                for row in connection.execute(
                    "SELECT book_id FROM playlist_books WHERE playlist_id=? ORDER BY position",
                    (playlist_id,),
                )
            ]
            return result

    @app.delete("/api/playlists/{playlist_id}", status_code=204)
    def delete_playlist(playlist_id: str, request: Request) -> Response:
        user(request)
        with db.connect() as connection:
            connection.execute("DELETE FROM playlist_books WHERE playlist_id=?", (playlist_id,))
            cursor = connection.execute("DELETE FROM playlists WHERE id=?", (playlist_id,))
        if not cursor.rowcount:
            raise HTTPException(404, "Playlist not found")
        return Response(status_code=204)

    @app.post("/api/history/{book_id}", status_code=204)
    def record_history(book_id: str, request: Request, payload: HistoryBody) -> Response:
        user(request)
        body = payload.model_dump()
        track_id = body.get("track_id") if isinstance(body, dict) else None
        if not isinstance(track_id, str):
            raise HTTPException(422, "track_id is required")
        book = db.book(book_id)
        if book is None or not any(track["id"] == track_id for track in book["tracks"]):
            raise HTTPException(422, "track does not belong to this book")
        selected = next(track for track in book["tracks"] if track["id"] == track_id)
        if selected.get("duration") and payload.position > selected["duration"] + 2:
            raise HTTPException(422, "position is outside track duration")
        db.record_history(book_id, track_id, payload.position)
        return Response(status_code=204)

    @app.get("/api/directories")
    def directories(request: Request) -> list[dict[str, str]]:
        user(request)
        return db.scanned_directories()

    @app.put("/api/settings/excluded-directories")
    def update_excluded_directories(request: Request, payload: ExclusionsBody) -> dict[str, Any]:
        user(request)
        body = payload.model_dump()
        paths = body.get("paths") if isinstance(body, dict) else None
        if (
            not isinstance(paths, list)
            or len(paths) > 500
            or any(not isinstance(path, str) for path in paths)
        ):
            raise HTTPException(422, "paths must be a list of at most 500 directory paths")
        available = {item["path"]: item for item in db.scanned_directories()}
        if len(set(paths)) != len(paths) or any(path not in available for path in paths):
            raise HTTPException(422, "excluded paths must be unique scanned directories")
        if not scanner.lock.acquire(blocking=False):
            raise HTTPException(
                409, "Wait for the active library scan to finish before changing exclusions"
            )
        chosen = [available[path] for path in paths]
        chosen = [
            item
            for item in chosen
            if not any(
                item["path"].startswith(parent["path"].rstrip("/") + "/")
                for parent in chosen
                if parent["path"] != item["path"]
            )
        ]
        try:
            db.replace_excluded_directories(chosen)
        finally:
            scanner.lock.release()
        return {"excluded_directories": chosen}

    @app.get("/api/settings/excluded-directories")
    def get_excluded_directories(request: Request) -> dict[str, Any]:
        user(request)
        return {"excluded_directories": db.excluded_directories()}

    @app.get("/api/v1/storage", response_model=StorageResponse)
    @app.get("/api/storage", response_model=StorageResponse)
    def storage(request: Request) -> dict[str, Any]:
        user(request)
        with db.connect() as connection:
            return storage_info(connection)

    def storage_info(connection: sqlite3.Connection) -> dict[str, Any]:
        row = connection.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM tracks").fetchone()
        formats = connection.execute(
            "SELECT mime_type, COUNT(*) FROM tracks GROUP BY mime_type ORDER BY COUNT(*) DESC"
        ).fetchall()
        with cache.lock:
            cached_bytes = sum(
                path.stat().st_size
                for path in cache.path.iterdir()
                if path.is_file() and not path.name.endswith(".part")
            )
        return {
            "tracks": row[0],
            "bytes": row[1],
            "source_bytes": row[1],
            "cached_bytes": cached_bytes,
            "cache_budget_bytes": cache.max_bytes,
            "cache_enabled": config.cache_warm_enabled and cache.max_bytes > 0,
            "formats": [{"mime_type": item[0] or "unknown", "count": item[1]} for item in formats],
        }

    @app.get("/api/v1/books/{book_id}/progress", response_model=ProgressResponse | None)
    @app.get("/api/progress/{book_id}", response_model=ProgressResponse | None)
    def get_progress(book_id: str, request: Request) -> dict[str, Any] | None:
        user(request)
        book = db.book(book_id, include_chapters=False)
        if book is None:
            raise HTTPException(404, "Book not found")
        return book["progress"]

    @app.get("/api/v1/tracks/{track_id}/chapters", response_model=ChapterPage)
    @app.get("/api/tracks/{track_id}/chapters", response_model=ChapterPage)
    def track_chapters(
        track_id: str, request: Request, offset: int = 0, limit: int = 50
    ) -> dict[str, Any]:
        user(request)
        if offset < 0 or not 1 <= limit <= 100:
            raise HTTPException(422, "offset must be non-negative and limit must be 1..100")
        return db.chapter_page(track_id, offset, limit)

    @app.head("/api/tracks/{track_id}/audio")
    def audio_head(track_id: str, request: Request) -> Response:
        user(request)
        track = db.track(track_id)
        if track is None:
            raise HTTPException(404, "Track not found")
        size = int(track.get("size") or 0)
        if size <= 0:
            raise HTTPException(502, "Drive did not report the audio file size")
        return Response(
            status_code=200,
            media_type=_mime_type(track),
            headers={"Content-Length": str(size), "Accept-Ranges": "bytes"},
        )

    @app.post("/api/tracks/{track_id}/warm", response_model=WarmResponse)
    def warm_audio(track_id: str, request: Request) -> JSONResponse:
        user(request)
        track = db.track(track_id)
        if track is None:
            raise HTTPException(404, "Track not found")
        if cache.file(track) is not None:
            status, code = "available", 200
        elif not config.cache_warm_enabled or cache.max_bytes <= 0:
            status, code = "disabled", 200
        elif int(track.get("size") or 0) > cache.max_bytes:
            status, code = "too_large", 200
        elif not schedule_warm(track):
            status, code = "busy", 200
        else:
            status, code = "queued", 202
        return JSONResponse({"status": status}, status_code=code)

    @app.get("/api/tracks/{track_id}/audio")
    def audio(track_id: str, request: Request) -> Response:
        user(request)
        track = db.track(track_id)
        if track is None:
            raise HTTPException(404, "Track not found")
        size = int(track.get("size") or 0)
        if size <= 0:
            raise HTTPException(502, "Drive did not report the audio file size")
        try:
            start, end = _range_header(request.headers.get("range", f"bytes=0-{size - 1}"), size)
        except ValueError:
            return Response(
                status_code=416,
                headers={"Content-Range": f"bytes */{size}", "Accept-Ranges": "bytes"},
            )
        cached_stream = None
        with cache.lock:
            cached = cache.file(track)
            if cached is not None:
                with suppress(OSError):
                    cached_stream = cache.serve(cached, start, end - start + 1)
        mime = _mime_type(track)
        if cached_stream is not None:
            length = end - start + 1
            headers = {
                "Accept-Ranges": "bytes",
                "Content-Length": str(length),
                "Cache-Control": "private, no-store",
            }
            status = 200
            if start != 0 or end != size - 1:
                headers["Content-Range"] = f"bytes {start}-{end}/{size}"
                status = 206
            return CachedResponse(
                cached_stream,
                status_code=status,
                media_type=mime,
                headers=headers,
            )
        auth = StoredDriveAuth(db, config.credentials_path)
        client = httpx.Client(follow_redirects=True)
        url = f"{DRIVE_API}/{track_id}?alt=media&supportsAllDrives=true"
        try:
            fetcher = HttpRangeFetcher.open(
                url, size=size, client=client, auth=auth, name=track["name"]
            )
            reader = SeekableBlockReader(fetcher, block_size=64 * 1024, name=track["name"])
        except Exception:
            client.close()
            raise
        length = end - start + 1
        full_read = start == 0 and end == size - 1 and size <= cache.max_bytes

        def chunks() -> Iterator[bytes]:
            drive_read_ms = 0.0
            drive_reads = 0
            bytes_yielded = 0
            try:
                # Spool complete-file requests to disk while yielding bounded chunks.
                # Partial/disconnected streams never publish an incomplete cache entry.
                with ExitStack() as stack:
                    spool = None
                    if full_read:
                        try:
                            spool = stack.enter_context(tempfile.TemporaryFile(dir=cache.path))
                        except OSError:
                            logger.info("Cannot spool audio cache track=%s", track_id)
                    reader.seek(start)
                    remaining = length
                    while remaining:
                        read_started = time.perf_counter()
                        chunk = reader.read(min(64 * 1024, remaining))
                        drive_read_ms += (time.perf_counter() - read_started) * 1000
                        if not chunk:
                            break
                        drive_reads += 1
                        bytes_yielded += len(chunk)
                        remaining -= len(chunk)
                        if spool is not None:
                            try:
                                spool.write(chunk)
                            except OSError:
                                logger.info("Audio cache spool write failed track=%s", track_id)
                                spool = None
                        yield chunk
                    if spool is not None and bytes_yielded == size:
                        spool.seek(0)
                        cache.put(track, iter(lambda: spool.read(64 * 1024), b""))
            except FetchError as exc:
                raise HTTPException(502, f"Drive audio stream failed: {exc}") from exc
            finally:
                logger.info(
                    "Drive stream metrics reads=%d read_ms=%d bytes=%d",
                    drive_reads,
                    round(drive_read_ms),
                    bytes_yielded,
                )
                reader.close()
                client.close()

        mime = _mime_type(track)
        upstream_name = track["name"].lower()
        if upstream_name.endswith((".m4a", ".m4b", ".m4p", ".mp4")):
            mime = "audio/mp4"
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Length": str(length),
            "Cache-Control": "private, no-store",
        }
        if start != 0 or end != size - 1:
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
            status = 206
        else:
            status = 200
        return StreamingResponse(chunks(), status_code=status, media_type=mime, headers=headers)

    @app.post("/api/tracks/{track_id}/playback-metrics", status_code=204)
    def playback_metrics(track_id: str, request: Request, payload: MetricsBody) -> Response:
        user(request)
        if db.track(track_id) is None:
            raise HTTPException(404, "Track not found")
        body = payload.model_dump()
        fields = {
            "startupMs": (0, 600_000),
            "stalls": (0, 10_000),
            "bufferedAhead": (0, 86_400),
            "rangeMs": (0, 86_400_000),
            "rangeBytes": (0, 1_000_000_000_000),
            "ranges": (0, 100_000),
        }
        if not isinstance(body, dict) or any(
            type(body.get(key)) is not int or not low <= body[key] <= high
            for key, (low, high) in fields.items()
        ):
            raise HTTPException(422, "invalid playback metrics")
        if body["ranges"] and body["rangeBytes"] > body["ranges"] * (16 * 1024 * 1024):
            raise HTTPException(422, "rangeBytes exceeds per-session limit")
        logger.info(
            "Playback metrics startup_ms=%d stalls=%d buffered_ahead_s=%d ranges=%d",
            body["startupMs"],
            body["stalls"],
            body["bufferedAhead"],
            body["ranges"],
        )
        return Response(status_code=204)

    @app.get("/api/tracks/{track_id}/cover")
    def cover(track_id: str, request: Request) -> Response:
        user(request)
        track = db.track(track_id)
        if not track:
            raise HTTPException(404, "Track not found")
        remote = RemoteFile(
            id=track["id"],
            name=track["name"],
            path=track["path"],
            size=track["size"],
            mime_type=track["mime_type"],
        )
        source = DriveSource(
            auth=StoredDriveAuth(db, config.credentials_path),
            file_id=remote.id,
            budget_bytes=4 * 1024 * 1024,
        )
        try:
            stream = source.open(remote)
            try:
                from .probe import cover_bytes

                data, mime = cover_bytes(stream)
            finally:
                stream.close()
                source.close()
        except (DriveError, FetchError, ValueError) as exc:
            logger.exception("Cover fetch failed track_id=%s", track_id)
            raise HTTPException(502, "Could not fetch cover from Google Drive") from exc
        except IndexError as exc:
            raise HTTPException(404, "Cover not found") from exc
        if not data:
            raise HTTPException(404, "Cover not found")
        return Response(
            data,
            media_type=mime or "application/octet-stream",
            headers={"Cache-Control": "private, max-age=3600"},
        )

    static_dir = Path(__file__).with_name("static")

    @app.get("/", response_class=HTMLResponse)
    def index() -> FileResponse:
        return FileResponse(static_dir / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/static/{asset_path:path}")
    def static_asset(asset_path: str) -> FileResponse:
        path = (static_dir / asset_path).resolve()
        if not path.is_relative_to(static_dir.resolve()) or not path.is_file():
            raise HTTPException(404, "Asset not found")
        return FileResponse(path, headers={"Cache-Control": "public, max-age=31536000, immutable"})

    @app.get("/manifest.webmanifest")
    def manifest() -> JSONResponse:
        return JSONResponse(
            {
                "name": "RuangDengar — Private audiobook player",
                "short_name": "RuangDengar",
                "start_url": "/",
                "display": "standalone",
                "background_color": "#101010",
                "theme_color": "#101010",
                "icons": [
                    {
                        "src": "/icon.svg",
                        "sizes": "any",
                        "type": "image/svg+xml",
                        "purpose": "any maskable",
                    }
                ],
            },
            media_type="application/manifest+json",
        )

    @app.get("/icon.svg")
    def icon() -> Response:
        return Response(_ICON, media_type="image/svg+xml")

    @app.get("/sw.js")
    def service_worker() -> Response:
        return Response(
            _SERVICE_WORKER,
            media_type="application/javascript",
            headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
        )

    @app.get("/favicon.ico")
    def favicon() -> Response:
        return Response(_ICON, media_type="image/svg+xml")

    @app.get("/{client_path:path}", response_class=HTMLResponse)
    def client_route(client_path: str) -> FileResponse:
        if client_path == "api" or client_path.startswith("api/"):
            raise HTTPException(404, "API route not found")
        return FileResponse(static_dir / "index.html", headers={"Cache-Control": "no-cache"})

    return app


_ICON = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
<rect width="512" height="512" rx="112" fill="#101010"/>
<path d="M256 54a202 202 0 1 0 0 404 202 202 0 0 0 0-404zm-58 295V163l178 93z" fill="#b5f36d"/>
</svg>"""
_SERVICE_WORKER = """self.addEventListener('install', event => event.waitUntil(
  caches.open('audiobooks-v1').then(cache => cache.addAll([
    '/', '/manifest.webmanifest', '/icon.svg'
  ]))
));
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  const isStaticGet = url.origin === location.origin
    && event.request.method === 'GET'
    && !url.pathname.startsWith('/api/')
    && url.pathname !== '/';
  if (isStaticGet) {
    event.respondWith(
      caches.match(event.request).then(response => response || fetch(event.request))
    );
  }
});"""

_INDEX = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
  <meta name="theme-color" content="#fff8e7">
  <link rel="manifest" href="/manifest.webmanifest">
  <title>RuangDengar — Private audiobook player</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #101010;
      --panel: #1c1c1e;
      --text: #f5f5f7;
      --muted: #a1a1a6;
      --accent: #b5f36d;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font: 16px system-ui, -apple-system, sans-serif;
      padding-bottom: calc(94px + env(safe-area-inset-bottom));
    }
    header {
      position: sticky;
      top: 0;
      background: #101010ee;
      backdrop-filter: blur(15px);
      padding: 18px 20px 12px;
      z-index: 2;
    }
    h1 { font-size: 28px; margin: 0 0 14px; }
    input {
      width: 100%;
      border: 0;
      border-radius: 14px;
      background: var(--panel);
      color: var(--text);
      padding: 14px;
      font: inherit;
    }
    .layout {
      display: grid;
      grid-template-columns: minmax(260px, 0.8fr) minmax(0, 1.2fr);
      gap: 20px;
      padding: 8px 18px 24px;
    }
    #detail { min-width: 0; }
    #detail h2 { margin: 8px 0; }
    .content { padding: 12px 0; border-bottom: 1px solid #29292c; }
    .content-title { font-weight: 650; margin-bottom: 8px; }
    .chapter { padding: 8px 12px; color: var(--muted); cursor: pointer; }
    .chapter:hover, .content-play { color: var(--accent); }
    .back { display: none; }
    @media (max-width: 700px) {
      .layout { display: block; }
      #detail { display: none; }
      .layout.detail-open #library { display: none; }
      .layout.detail-open #detail { display: block; }
      .layout.detail-open #search { display: none; }
      .layout.detail-open .back { display: inline; }
    }
    .row {
      display: flex;
      gap: 14px;
      align-items: center;
      padding: 12px 2px;
      border-bottom: 1px solid #29292c;
    }
    .cover {
      width: 68px;
      height: 68px;
      border-radius: 9px;
      object-fit: cover;
      background: linear-gradient(135deg, #384d2b, #243b55);
      flex: none;
    }
    .meta { min-width: 0; flex: 1; }
    .meta strong, .meta span {
      display: block;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .meta span { color: var(--muted); font-size: 14px; margin-top: 5px; }
    .button {
      border: 0;
      border-radius: 12px;
      background: var(--accent);
      color: #121212;
      padding: 12px 16px;
      font-weight: 700;
    }
    #player {
      position: fixed;
      z-index: 3;
      bottom: 0;
      left: 0;
      right: 0;
      background: #202022f5;
      border-top: 1px solid #37373a;
      padding: 12px 18px calc(12px + env(safe-area-inset-bottom));
      backdrop-filter: blur(18px);
    }
    #now {
      font-size: 13px;
      color: var(--muted);
      margin-bottom: 8px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    audio { width: 100%; height: 38px; }
    #chapters { padding: 0 18px; }
    .empty { padding: 30px 6px; color: var(--muted); line-height: 1.5; }
    .top { display: flex; justify-content: space-between; align-items: center; }
    .link { border: 0; color: var(--accent); background: none; font: inherit; }
  </style>
  </style>
</head>
<body>
  <header>
    <div class="top">
      <button class="link back" id="back" type="button">‹ Library</button>
      <h1 id="heading">Your library</h1>
      <a class="link" id="signin" href="/auth/google">Sign in</a>
    </div>
    <input id="search" placeholder="Search books and authors" autocomplete="off">
    <p id="scan-status" role="status" aria-live="polite"></p>
  </header>
  <main class="layout" id="layout">
    <section id="library">
      <div class="empty">Sign in to connect your private audiobook library.</div>
    </section>
    <section id="detail" aria-live="polite">
      <div class="empty">Choose an album to view its contents.</div>
    </section>
  </main>
  <div id="player">
    <div id="now">Choose a book to start listening</div>
    <audio id="audio" controls preload="metadata"></audio>
  </div>
  <script>
    const $ = selector => document.querySelector(selector);
    const esc = value => String(value ?? '').replace(/[&<>"']/g, char =>
      ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[char]));
    let books = [], active = null, lastSave = 0;
    $('#signin').href = new URL('/auth/google', window.location.origin).href;
    async function api(url, options = {}) {
      const response = await fetch(url, {credentials: 'same-origin', ...options});
      if (response.status === 401) {
        $('#library').innerHTML = '<div class="empty">Sign in to view your library.</div>';
        throw Error('Sign in required');
      }
      if (!response.ok) throw Error(await response.text());
      return response.json();
    }
    async function load() {
      try {
        books = await api('/api/library');
        $('#signin').textContent = 'Refresh';
        $('#signin').href = '#';
        $('#signin').onclick = event => { event.preventDefault(); startScan(); };
        render();
        startScan();
        const status = await fetch('/api/library/scan', {credentials: 'same-origin'});
        if (status.ok) {
          const scan = await status.json();
          showScan(scan);
          if (scan.status === 'running') watchScan();
        }
      } catch (error) {
        if (error.message !== 'Sign in required') {
          $('#library').innerHTML =
            '<div class="empty">Could not load library. Check server settings.</div>';
        }
      }
    }
    function showScan(status) {
      if (status.status === 'idle') return;
      $('#scan-status').textContent = status.error ||
        `${status.status}: ${status.processed}/${status.total} · ${status.current || ''}`;
      if (['completed', 'failed', 'interrupted'].includes(status.status) && scanEvents) {
        scanEvents.close();
      }
    }
    let scanEvents = null;
    function watchScan() {
      if (scanEvents) scanEvents.close();
      scanEvents = new EventSource('/api/library/events');
      scanEvents.onmessage = event => {
        const status = JSON.parse(event.data);
        showScan(status);
        if (['completed', 'failed'].includes(status.status)) {
          scanEvents.close();
          load();
        }
      };
    }
    async function startScan() {
      try {
        const response = await fetch('/api/library/refresh', {
          method: 'POST', credentials: 'same-origin'
        });
        if (response.ok) watchScan();
        else if (response.status === 409) watchScan();
        else {
          $('#scan-status').textContent = await response.text();
        }
      } catch (error) {
        $('#scan-status').textContent = 'Could not start library scan.';
      }
    }
    function render() {
      $('#heading').textContent = 'Your library';
      const query = $('#search').value.toLowerCase();
      const filtered = books.filter(book =>
        (book.title + ' ' + (book.artist || '')).toLowerCase().includes(query));
      $('#library').innerHTML = filtered.length ? filtered.map(book =>
        `<article class="row" data-id="${esc(book.id)}">
          <img class="cover" src="${esc(book.cover || '')}"
               onerror="this.style.visibility='hidden'">
          <div class="meta"><strong>${esc(book.title)}</strong>
            <span>${esc(book.artist || 'Audiobook')} ·
              ${Math.round((book.duration || 0) / 60)} min</span>
            <span>${book.progress ? 'Resume · ' + Math.floor(book.progress.position / 60) + ' min' :
              'Tap to view'}</span>
          </div><button class="button play">Open</button></article>`).join('') :
          '<div class="empty">No matching books.</div>';
      document.querySelectorAll('.row').forEach(row => {
        row.onclick = () => play(row.dataset.id);
        row.querySelector('.play').onclick = event => {
          event.stopPropagation();
          play(row.dataset.id);
        };
      });
    }
    function play(id) {
      active = books.find(book => book.id === id);
      if (!active) return;
      $('#layout').classList.add('detail-open');
      $('#search').value = '';
      $('#heading').textContent = active.title;
      const tracks = active.tracks || [];
      $('#detail').innerHTML = `<img class="cover" src="${esc(active.cover || '')}"
        onerror="this.style.visibility='hidden'"><h2>${esc(active.title)}</h2>
        <p>${esc(active.artist || 'Audiobook')} · ${tracks.length} items
        </p>
        ` + tracks.map(track => `<article class="content">
          <div class="content-title">${esc(track.title || track.name)}</div>
          <button class="link content-play" data-track="${esc(track.id)}">Play</button>
          ${(track.chapters || []).map((chapter, index) =>
            `<button class="link chapter" data-track="${esc(track.id)}"
              data-start="${Number(chapter.start) || 0}">
              ${esc(chapter.title || ('Chapter ' + (index + 1)))}</button>`).join('')}
        </article>`).join('');
      document.querySelectorAll('#detail .content-play').forEach(button => {
        button.onclick = () => startTrack(tracks.find(track => track.id === button.dataset.track));
      });
      document.querySelectorAll('#detail .chapter').forEach(button => {
        button.onclick = () => startTrack(
          tracks.find(track => track.id === button.dataset.track), Number(button.dataset.start)
        );
      });
      const resume = tracks.find(track => track.id === active.progress?.track_id);
      if (resume) startTrack(resume, active.progress.position);
    }
    function startTrack(track, position = 0) {
      if (!track) return;
      $('#now').textContent = active.title + ' · ' + (track.title || track.name);
      const player = $('#audio');
      player.src = '/api/tracks/' + encodeURIComponent(track.id) + '/audio';
      player.onloadedmetadata = () => { if (position > 0) player.currentTime = position; };
      player.onended = () => {
        const index = active.tracks.findIndex(item => item.id === track.id);
        const next = active.tracks[index + 1];
        if (next) startTrack(next);
      };
      player.play().catch(() => {});
      player.ontimeupdate = () => {
        if (active && player.currentTime > 0 && Date.now() - lastSave > 10000) {
          save(track.id, player.currentTime);
        }
      };
    }
    async function save(trackId, position, bookId = active?.id) {
      if (!bookId) return;
      lastSave = Date.now();
      try {
        await fetch('/api/progress/' + encodeURIComponent(bookId), {
          method: 'PUT', credentials: 'same-origin',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({track_id: trackId, position})
        });
        if (active?.id === bookId) active.progress = {track_id: trackId, position};
      } catch (error) {}
    }
    if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js');
    $('#back').onclick = () => {
      $('#layout').classList.remove('detail-open');
      $('#heading').textContent = 'Your library';
    };
    $('#search').oninput = render;
    document.addEventListener('visibilitychange', () => {
      if (document.hidden && active) {
        const track = active.tracks.find(item =>
          $('#audio').src.includes(encodeURIComponent(item.id)));
        if (track) save(track.id, $('#audio').currentTime);
      }
    });
    load();
  </script>
</body>
</html>"""


def main() -> None:
    import uvicorn

    config = WebConfig()
    config.check()
    app = create_app(config)
    uvicorn.run(
        app, host=os.getenv("APP_HOST", "127.0.0.1"), port=int(os.getenv("APP_PORT", "8000"))
    )


app = create_app()
