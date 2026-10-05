"""SQLite catalog migration and projections used by both API versions."""

from __future__ import annotations

import base64
import json
import math
import sqlite3
from contextlib import AbstractContextManager, nullcontext
from typing import Any

from fastapi import HTTPException


def timestamp(db: sqlite3.Connection) -> str:
    return str(db.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')").fetchone()[0])


def bump_revision(db: sqlite3.Connection) -> None:
    db.execute("UPDATE catalog_state SET revision=revision+1 WHERE id=1")


def write_projection(
    db: sqlite3.Connection, books: list[dict[str, Any]], tracks: list[dict[str, Any]]
) -> None:
    """Upsert stable identities; chapters have one storage owner, not two JSON copies."""
    now = timestamp(db)
    known = {row[0]: row[1] for row in db.execute("SELECT id,added_at FROM books")}
    by_id = {track["id"]: track for track in tracks}
    for book in books:
        summary = {
            key: value
            for key, value in book.items()
            if key not in {"progress", "favorite", "rating", "tags"}
        }
        summary["added_at"] = known.get(book["id"]) or book.get("added_at") or now
        summaries = []
        for ordinal, item in enumerate(book.get("tracks", [])):
            track = dict(by_id.get(item["id"], item))
            chapters = item.get("chapters", []) or track.get("chapters", [])
            track.update(ordinal=ordinal, chapter_count=len(chapters), chapters=[])
            public_track = {
                **item,
                "ordinal": ordinal,
                "chapter_count": len(chapters),
                "chapters": [],
            }
            summaries.append(public_track)
            db.execute(
                "INSERT INTO tracks(id,book_id,name,path,size,mime_type,data,ordinal,duration) "
                "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "book_id=excluded.book_id,name=excluded.name,path=excluded.path,"
                "size=excluded.size,mime_type=excluded.mime_type,data=excluded.data,"
                "ordinal=excluded.ordinal,duration=excluded.duration",
                (
                    track["id"],
                    book["id"],
                    track["name"],
                    track.get("path", ""),
                    track.get("size") or 0,
                    track.get("mime_type"),
                    json.dumps(track),
                    ordinal,
                    track.get("duration") or item.get("duration") or 0,
                ),
            )
            db.execute("DELETE FROM chapters WHERE track_id=?", (track["id"],))
            db.executemany(
                "INSERT INTO chapters(track_id,ordinal,data) VALUES(?,?,?)",
                [(track["id"], i, json.dumps(chapter)) for i, chapter in enumerate(chapters)],
            )
        summary["tracks"] = summaries
        summary["track_count"] = len(summaries)
        db.execute(
            "INSERT INTO books(id,title,artist,cover,duration,data,added_at,track_count) "
            "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
            "title=excluded.title,artist=excluded.artist,cover=excluded.cover,"
            "duration=excluded.duration,data=excluded.data,track_count=excluded.track_count,"
            "added_at=CASE WHEN books.added_at='' THEN excluded.added_at ELSE books.added_at END",
            (
                summary["id"],
                summary["title"],
                summary.get("artist"),
                summary.get("cover"),
                summary.get("duration") or 0,
                json.dumps(summary),
                summary["added_at"],
                len(summaries),
            ),
        )


def migrate_catalog(db: sqlite3.Connection) -> None:
    for table, additions in {
        "books": {
            "added_at": "TEXT NOT NULL DEFAULT ''",
            "track_count": "INTEGER NOT NULL DEFAULT 0",
        },
        "tracks": {"ordinal": "INTEGER NOT NULL DEFAULT 0", "duration": "REAL NOT NULL DEFAULT 0"},
        "progress": {
            "revision": "INTEGER NOT NULL DEFAULT 0",
            "completed": "INTEGER NOT NULL DEFAULT 0",
        },
        "playlists": {"revision": "INTEGER NOT NULL DEFAULT 0"},
        "scan_state": {"job_id": "TEXT"},
    }.items():
        columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        for name, declaration in additions.items():
            if name not in columns:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
    # Do not use executescript here: migration/backfill must be one transaction.
    for statement in (
        "CREATE TABLE IF NOT EXISTS catalog_state(id INTEGER PRIMARY KEY CHECK(id=1), "
        "revision INTEGER NOT NULL DEFAULT 0, schema_version INTEGER NOT NULL DEFAULT 0)",
        "INSERT OR IGNORE INTO catalog_state(id) VALUES(1)",
        "CREATE TABLE IF NOT EXISTS chapters(track_id TEXT NOT NULL REFERENCES tracks(id) "
        "ON DELETE CASCADE, ordinal INTEGER NOT NULL, data TEXT NOT NULL, "
        "PRIMARY KEY(track_id,ordinal))",
        "CREATE TABLE IF NOT EXISTS progress_events(book_id TEXT NOT NULL REFERENCES books(id) "
        "ON DELETE CASCADE, event_id TEXT NOT NULL, "
        "payload TEXT NOT NULL, result TEXT NOT NULL, PRIMARY KEY(book_id,event_id))",
        "CREATE INDEX IF NOT EXISTS tracks_book_order ON tracks(book_id,ordinal)",
        "CREATE INDEX IF NOT EXISTS books_title_order ON books(title COLLATE NOCASE,id)",
        "CREATE INDEX IF NOT EXISTS books_added_order ON books(added_at,id)",
        "CREATE INDEX IF NOT EXISTS history_order ON listening_history(played_at,id)",
        "CREATE INDEX IF NOT EXISTS playlist_order ON playlist_books(playlist_id,position)",
        "CREATE INDEX IF NOT EXISTS tags_lookup ON book_tags(tag,book_id)",
    ):
        db.execute(statement)
    if db.execute("SELECT schema_version FROM catalog_state WHERE id=1").fetchone()[0] < 1:
        books = [json.loads(row[0]) for row in db.execute("SELECT data FROM books")]
        tracks = [json.loads(row[0]) for row in db.execute("SELECT data FROM tracks")]
        write_projection(db, books, tracks)
        db.execute(
            "UPDATE progress SET completed=1 WHERE EXISTS(SELECT 1 FROM tracks t "
            "WHERE t.id=progress.track_id AND t.duration>0 AND progress.position>=t.duration*0.95 "
            "AND t.ordinal=(SELECT MAX(ordinal) FROM tracks WHERE book_id=progress.book_id))"
        )
        db.execute("UPDATE catalog_state SET schema_version=1 WHERE id=1")


class CatalogQueries:
    def connect(self) -> AbstractContextManager[sqlite3.Connection]:
        raise NotImplementedError

    @staticmethod
    def enrich(db: sqlite3.Connection, books: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not books:
            return books
        db.execute("CREATE TEMP TABLE selected_books(id TEXT PRIMARY KEY)")
        db.executemany("INSERT INTO selected_books VALUES(?)", [(book["id"],) for book in books])
        favorites = {
            row[0]
            for row in db.execute("SELECT book_id FROM favorites JOIN selected_books ON id=book_id")
        }
        ratings = dict(
            db.execute("SELECT book_id,rating FROM ratings JOIN selected_books ON id=book_id")
        )
        tags: dict[str, list[str]] = {}
        for row in db.execute(
            "SELECT book_id,tag FROM book_tags JOIN selected_books ON id=book_id ORDER BY tag"
        ):
            tags.setdefault(row[0], []).append(row[1])
        progress = {
            row["book_id"]: dict(row)
            for row in db.execute(
                "SELECT progress.* FROM progress JOIN selected_books ON id=book_id"
            )
        }
        durations: dict[str, list[dict[str, Any]]] = {}
        for row in db.execute(
            "SELECT tracks.id,book_id,duration FROM tracks JOIN selected_books "
            "ON selected_books.id=book_id ORDER BY ordinal"
        ):
            durations.setdefault(row["book_id"], []).append(dict(row))
        for book in books:
            book.update(
                favorite=book["id"] in favorites,
                rating=ratings.get(book["id"]),
                tags=tags.get(book["id"], []),
                progress=progress.get(book["id"]),
            )
            state = book["progress"]
            if state:
                preceding = 0.0
                ordered_tracks = book.get("tracks") or durations.get(book["id"], [])
                for track in ordered_tracks:
                    if track["id"] == state["track_id"]:
                        break
                    preceding += track.get("duration") or 0
                state["completed"] = bool(state["completed"])
                state["book_position"] = preceding + state["position"]
                duration = book.get("duration") or 0
                if any(not track.get("duration") for track in ordered_tracks):
                    duration = 0
                state["fraction"] = (
                    min(1, state["book_position"] / duration) if duration > 0 else None
                )
                state["status"] = (
                    "completed"
                    if state["completed"]
                    else "in-progress"
                    if state["book_position"] > 0
                    else "not-started"
                )
        db.execute("DROP TABLE selected_books")
        return books

    def library(self, connection: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
        with nullcontext(connection) if connection is not None else self.connect() as db:
            if not db.in_transaction:
                db.execute("BEGIN")
            books = [
                json.loads(row[0])
                for row in db.execute("SELECT data FROM books ORDER BY title COLLATE NOCASE,id")
            ]
            return self.enrich(db, books)

    def book(self, book_id: str, include_chapters: bool = True) -> dict[str, Any] | None:
        with self.connect() as db:
            db.execute("BEGIN")
            row = db.execute("SELECT data FROM books WHERE id=?", (book_id,)).fetchone()
            if row is None:
                return None
            book = self.enrich(db, [json.loads(row[0])])[0]
            if include_chapters:
                chapters: dict[str, list[Any]] = {}
                for item in db.execute(
                    "SELECT chapters.* FROM chapters JOIN tracks ON tracks.id=track_id "
                    "WHERE book_id=? ORDER BY track_id,chapters.ordinal",
                    (book_id,),
                ):
                    chapters.setdefault(item["track_id"], []).append(json.loads(item["data"]))
                for track in book.get("tracks", []):
                    track["chapters"] = chapters.get(track["id"], [])
            return book

    def chapter_page(self, track_id: str, offset: int, limit: int) -> dict[str, Any]:
        with self.connect() as db:
            db.execute("BEGIN")
            if not db.execute(
                "SELECT 1 FROM tracks JOIN books ON books.id=book_id WHERE tracks.id=?", (track_id,)
            ).fetchone():
                raise HTTPException(404, "Track not found")
            rows = db.execute(
                "SELECT data FROM chapters WHERE track_id=? ORDER BY ordinal LIMIT ? OFFSET ?",
                (track_id, limit + 1, offset),
            ).fetchall()
            return {
                "track_id": track_id,
                "chapters": [json.loads(row[0]) for row in rows[:limit]],
                "next_offset": offset + limit if len(rows) > limit else None,
                "revision": db.execute("SELECT revision FROM catalog_state WHERE id=1").fetchone()[
                    0
                ],
            }

    def revision(self) -> int:
        with self.connect() as db:
            return int(db.execute("SELECT revision FROM catalog_state WHERE id=1").fetchone()[0])

    def catalog_page(
        self,
        *,
        q: str = "",
        sort: str = "title",
        direction: str = "asc",
        status: str = "all",
        favorite: bool = False,
        playlist_id: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        sorts = {
            "title": "b.title COLLATE NOCASE",
            "author": "COALESCE(b.artist,'') COLLATE NOCASE",
            "duration": "b.duration",
            "added": "b.added_at",
            "rating": "COALESCE(r.rating,0)",
        }
        if (
            sort not in sorts
            or direction not in {"asc", "desc"}
            or status not in {"all", "not-started", "in-progress", "completed"}
        ):
            raise HTTPException(422, "Invalid catalog filter or sort")
        signature = [q, sort, direction, status, favorite, playlist_id]
        with self.connect() as db:
            db.execute("BEGIN")
            revision = db.execute("SELECT revision FROM catalog_state WHERE id=1").fetchone()[0]
            after = None
            if cursor:
                try:
                    token = json.loads(base64.urlsafe_b64decode(cursor.encode()))
                    if token["query"] != signature:
                        raise ValueError
                    after = token["after"]
                    if (
                        not isinstance(after, list)
                        or len(after) != 2
                        or not isinstance(after[1], str)
                    ):
                        raise ValueError
                    if not isinstance(after[0], (str, int, float)) or isinstance(after[0], bool):
                        raise ValueError
                    if isinstance(after[0], float) and not math.isfinite(after[0]):
                        raise ValueError
                    if token["revision"] != revision:
                        raise HTTPException(409, "Catalog changed; restart pagination")
                except (ValueError, KeyError, TypeError, UnicodeError) as exc:
                    raise HTTPException(422, "Invalid cursor") from exc
            conditions = ["1=1"]
            values: list[Any] = []
            if q.strip():
                conditions.append(
                    "(instr(lower(b.title || ' ' || COALESCE(b.artist,'')),lower(?))>0 OR "
                    "EXISTS(SELECT 1 FROM book_tags WHERE book_id=b.id "
                    "AND instr(lower(tag),lower(?))>0))"
                )
                values.extend([q.strip(), q.strip()])
            if favorite:
                conditions.append("EXISTS(SELECT 1 FROM favorites WHERE book_id=b.id)")
            if playlist_id:
                conditions.append(
                    "EXISTS(SELECT 1 FROM playlist_books WHERE book_id=b.id AND playlist_id=?)"
                )
                values.append(playlist_id)
            if status == "completed":
                conditions.append("p.completed=1")
            elif status == "not-started":
                conditions.append(
                    "(p.book_id IS NULL OR (p.completed=0 AND p.position=0 AND "
                    "COALESCE((SELECT ordinal FROM tracks WHERE id=p.track_id),0)=0))"
                )
            elif status == "in-progress":
                conditions.append(
                    "p.completed=0 AND (p.position>0 OR "
                    "(SELECT ordinal FROM tracks WHERE id=p.track_id)>0)"
                )
            join = (
                " FROM books b LEFT JOIN progress p ON p.book_id=b.id "
                "LEFT JOIN ratings r ON r.book_id=b.id "
            )
            where = " WHERE " + " AND ".join(conditions)
            total = db.execute("SELECT COUNT(*)" + join + where, values).fetchone()[0]
            key = sorts[sort]
            if after:
                op = ">" if direction == "asc" else "<"
                where += f" AND ({key} {op} ? OR ({key} = ? AND b.id {op} ?))"
                values.extend([after[0], after[0], after[1]])
            rows = db.execute(
                "SELECT b.id,b.title,b.artist,b.cover,b.duration,b.added_at,b.track_count,"
                f"{key} AS sort_key,"
                "json_extract(b.data,'$.album') AS album,"
                "json_extract(b.data,'$.album_artist') AS album_artist,"
                "json_extract(b.data,'$.directory') AS directory"
                + join
                + where
                + f" ORDER BY {key} {direction},b.id {direction} LIMIT ?",
                [*values, limit + 1],
            ).fetchall()
            books = self.enrich(db, [dict(row) for row in rows[:limit]])
            for book in books:
                book.pop("tracks", None)
            next_cursor = None
            if len(rows) > limit:
                last = rows[limit - 1]
                next_cursor = base64.urlsafe_b64encode(
                    json.dumps(
                        {
                            "query": signature,
                            "revision": revision,
                            "after": [last["sort_key"], last["id"]],
                        }
                    ).encode()
                ).decode()
            return {
                "items": books,
                "next_cursor": next_cursor,
                "revision": revision,
                "total": total,
            }

    def checkpoint(
        self,
        book_id: str,
        track_id: str,
        position: float,
        *,
        base_revision: int | None = None,
        event_id: str | None = None,
        completed: bool | None = None,
    ) -> dict[str, Any]:
        payload = json.dumps([track_id, position, completed])
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM books WHERE id=?", (book_id,)).fetchone():
                raise HTTPException(404, "Book not found")
            track = db.execute(
                "SELECT duration FROM tracks WHERE id=? AND book_id=?", (track_id, book_id)
            ).fetchone()
            if track is None:
                raise HTTPException(422, "track does not belong to this book")
            if (
                not math.isfinite(position)
                or position < 0
                or (track[0] > 0 and position > track[0] + 2)
            ):
                raise HTTPException(422, "position is outside track duration")
            if event_id:
                prior = db.execute(
                    "SELECT payload,result FROM progress_events WHERE book_id=? AND event_id=?",
                    (book_id, event_id),
                ).fetchone()
                if prior:
                    if prior["payload"] != payload:
                        raise HTTPException(409, "Event ID already used for a different checkpoint")
                    return dict(json.loads(prior["result"]))
            current = db.execute("SELECT * FROM progress WHERE book_id=?", (book_id,)).fetchone()
            revision = current["revision"] if current else 0
            if base_revision is not None and base_revision != revision:
                raise HTTPException(
                    409, {"code": "progress_conflict", "current_revision": revision}
                )
            finished = (
                bool(completed) if completed is not None else bool(current and current["completed"])
            )
            now = timestamp(db)
            db.execute(
                "INSERT INTO progress(book_id,track_id,position,updated_at,revision,completed) "
                "VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(book_id) DO UPDATE SET track_id=excluded.track_id,"
                "position=excluded.position,"
                "updated_at=excluded.updated_at,revision=excluded.revision,completed=excluded.completed",
                (book_id, track_id, position, now, revision + 1, finished),
            )
            book = json.loads(
                db.execute("SELECT data FROM books WHERE id=?", (book_id,)).fetchone()[0]
            )
            result: dict[str, Any] = self.enrich(db, [book])[0]["progress"]
            if event_id:
                db.execute(
                    "INSERT INTO progress_events VALUES(?,?,?,?)",
                    (book_id, event_id, payload, json.dumps(result)),
                )
                db.execute(
                    "DELETE FROM progress_events WHERE book_id=? AND rowid NOT IN "
                    "(SELECT rowid FROM progress_events WHERE book_id=? "
                    "ORDER BY rowid DESC LIMIT 1000)",
                    (book_id, book_id),
                )
            bump_revision(db)
            return result
