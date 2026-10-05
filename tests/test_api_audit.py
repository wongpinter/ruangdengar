"""Contract, migration, and concurrency regressions from the frontend API audit."""

from __future__ import annotations

import base64
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import audioscan.web as web
from audioscan.web import Database, create_app
from test_web import config, seeded_db, sign_in


def client_for(tmp_path: Path) -> tuple[Database, TestClient]:
    cfg = config(tmp_path)
    db = seeded_db(cfg.db_path)
    client = TestClient(create_app(cfg, db))
    sign_in(client)
    return db, client


def add_books(db: Database, count: int) -> None:
    books, tracks = [], []
    for i in range(count):
        track = {
            "id": f"t{i}",
            "book_id": f"b{i}",
            "name": f"{i}.mp3",
            "path": f"Book/{i}.mp3",
            "duration": 60,
            "size": 16,
            "chapters": [],
        }
        books.append(
            {
                "id": f"b{i}",
                "title": "Same title",
                "artist": "Author",
                "duration": 60,
                "tracks": [track],
                "album": "Same album",
            }
        )
        tracks.append(track)
    db.save_library(books, tracks)


def test_legacy_database_backfills_once_and_preserves_state(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    track = {
        "id": "t",
        "book_id": "b",
        "name": "Part",
        "path": "Book/Part",
        "duration": 60,
        "chapters": [{"start": 0, "title": "Opening"}],
    }
    book = {"id": "b", "title": "Book", "duration": 60, "tracks": [track]}
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE books(id TEXT PRIMARY KEY,title TEXT NOT NULL,artist TEXT,cover TEXT,
                duration REAL NOT NULL DEFAULT 0,data TEXT NOT NULL);
            CREATE TABLE tracks(id TEXT PRIMARY KEY,book_id TEXT NOT NULL,name TEXT NOT NULL,
                path TEXT NOT NULL,size INTEGER NOT NULL DEFAULT 0,mime_type TEXT,data TEXT NOT NULL);
            CREATE TABLE progress(book_id TEXT PRIMARY KEY,track_id TEXT NOT NULL,
                position REAL NOT NULL DEFAULT 0,updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        """)
        connection.execute(
            "INSERT INTO books VALUES('b','Book',NULL,NULL,60,?)", (json.dumps(book),)
        )
        connection.execute(
            "INSERT INTO tracks VALUES('t','b','Part','Book/Part',16,NULL,?)", (json.dumps(track),)
        )
        connection.execute("INSERT INTO progress(book_id,track_id,position) VALUES('b','t',59)")
    db = Database(path)
    migrated = db.book("b")
    assert migrated is not None
    assert migrated["tracks"][0]["chapters"][0]["title"] == "Opening"
    assert migrated["progress"]["completed"] is True
    assert migrated["added_at"].endswith("Z")
    with db.connect() as connection:
        assert (
            connection.execute("SELECT added_at FROM books").fetchone()[0] == migrated["added_at"]
        )
        assert (
            json.loads(connection.execute("SELECT data FROM tracks").fetchone()[0])["chapters"]
            == []
        )
    assert Database(path).book("b") == migrated
    db.save_library([book], [track])
    assert db.book("b")["added_at"] == migrated["added_at"]
    assert db.book("b")["progress"]["completed"] is True


def test_consistent_book_contract_and_private_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db, client = client_for(tmp_path)
    db.save_progress("book-key", "drive-track", 10)
    book = db.book("book-key")
    book["tracks"][0]["chapters"] = [{"start": i, "title": str(i)} for i in range(50)]
    track = db.track("drive-track")
    track["probe_meta"] = {"secret_internal": True}
    db.save_library([book], [track])
    client.put("/api/books/book-key/favorite", json={"favorite": True})
    client.put("/api/books/book-key/rating", json={"rating": 4})
    client.put("/api/books/book-key/tags", json={"tags": ["Favorite"]})

    class Provider:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> Provider:
            return self

        async def __aexit__(self, *args: Any) -> None:
            pass

        async def get(self, url: str) -> httpx.Response:
            return httpx.Response(
                200, json={"volumeInfo": {"title": "Updated"}}, request=httpx.Request("GET", url)
            )

    monkeypatch.setattr(web.httpx, "AsyncClient", Provider)
    applied = client.post(
        "/api/books/book-key/metadata/apply", json={"source": "Google Books", "source_id": "x"}
    )
    assert applied.status_code == 200
    detail = client.get("/api/books/book-key").json()
    assert applied.json() == detail
    listed = client.get("/api/library").json()[0]
    assert listed == detail
    bootstrap = client.get("/api/bootstrap").json()["books"][0]
    assert bootstrap == detail
    assert detail["favorite"] and detail["rating"] == 4 and detail["tags"] == ["Favorite"]
    assert detail["progress"]["position"] == 10
    assert detail["tracks"][0]["chapter_count"] == 50
    assert detail["tracks"][0]["chapters"] == []
    assert "metadata_overrides" not in detail
    assert "probe_meta" not in detail["tracks"][0]
    schema = client.get("/openapi.json").json()
    assert schema["paths"]["/api/books/{book_id}"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]["$ref"].endswith("BookResponse")


def test_chapters_query_only_page_and_cascade_removed_tracks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db, client = client_for(tmp_path)
    book = db.book("book-key")
    book["tracks"][0]["chapters"] = [{"start": i, "title": str(i)} for i in range(60)]
    db.save_library([book], [db.track("drive-track")])
    monkeypatch.setattr(
        db, "book", lambda *args, **kwargs: pytest.fail("Chapter page must not load a book")
    )
    monkeypatch.setattr(
        db, "track", lambda *args, **kwargs: pytest.fail("Chapter page must not decode full track")
    )
    page = client.get("/api/v1/tracks/drive-track/chapters?offset=50&limit=10").json()
    assert len(page["chapters"]) == 10 and page["next_offset"] is None
    assert page["chapters"][0]["title"] == "50"
    db.save_library([], [])
    with db.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM chapters").fetchone()[0] == 0


def test_catalog_cursor_ties_filters_and_revision_conflicts(tmp_path: Path) -> None:
    db, client = client_for(tmp_path)
    add_books(db, 6)
    ids, cursor = [], None
    while True:
        params = {"limit": 2, "sort": "title", "direction": "desc"}
        if cursor:
            params["cursor"] = cursor
        response = client.get("/api/v1/books", params=params)
        assert response.status_code == 200
        page = response.json()
        assert page["total"] == 6
        assert all("tracks" not in item for item in page["items"])
        ids.extend(item["id"] for item in page["items"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert ids == [f"b{i}" for i in reversed(range(6))]
    first = client.get("/api/v1/books?limit=2").json()
    client.put("/api/books/b0/favorite", json={"favorite": True})
    assert (
        client.get("/api/v1/books", params={"limit": 2, "cursor": first["next_cursor"]}).status_code
        == 409
    )
    assert [item["id"] for item in client.get("/api/v1/books?favorite=true").json()["items"]] == [
        "b0"
    ]
    db.save_progress("b1", "t1", 20)
    assert client.get("/api/v1/books?status=in-progress").json()["total"] == 1
    assert client.get("/api/v1/books?status=not-started").json()["total"] == 5
    assert client.get("/api/v1/books?limit=101").status_code == 422
    token = base64.urlsafe_b64encode(
        json.dumps(
            {
                "query": ["", "title", "asc", "all", False, None],
                "after": [{}, "b0"],
                "revision": db.revision(),
            }
        ).encode()
    ).decode()
    assert client.get("/api/v1/books", params={"cursor": token}).status_code == 422


def test_progress_conditional_rewind_idempotency_completion_and_duration(tmp_path: Path) -> None:
    db, client = client_for(tmp_path)
    path = "/api/v1/books/book-key/progress"
    body = {"track_id": "drive-track", "position": 40, "base_revision": 0, "event_id": "first"}
    first = client.put(path, json=body)
    assert first.status_code == 200 and first.json()["revision"] == 1
    assert client.put(path, json=body).json() == first.json()
    conflict = client.put(path, json={**body, "position": 10, "event_id": "stale"})
    assert conflict.status_code == 409
    assert db.book("book-key")["progress"]["position"] == 40
    rewind = client.put(
        path, json={**body, "position": 10, "event_id": "rewind", "base_revision": 1}
    )
    assert rewind.status_code == 200 and rewind.json()["position"] == 10
    assert client.put(path, json={**body, "position": 11}).status_code == 409
    assert (
        client.put(
            path, json={**body, "position": 1000, "event_id": "outside", "base_revision": 2}
        ).status_code
        == 422
    )
    finished = client.put(
        path, json={**body, "base_revision": 2, "event_id": "done", "completed": True}
    )
    assert finished.json()["status"] == "completed"
    assert client.get("/api/v1/books?status=completed").json()["total"] == 1
    assert (
        client.put(
            "/api/progress/book-key", json={"track_id": "drive-track", "position": 10}
        ).status_code
        == 428
    )
    assert client.get(path).json() == finished.json()


def test_concurrent_checkpoint_compare_and_swap_has_one_winner(tmp_path: Path) -> None:
    db, _ = client_for(tmp_path)

    def save(event: str) -> int:
        try:
            return db.checkpoint("book-key", "drive-track", 10, base_revision=0, event_id=event)[
                "revision"
            ]
        except HTTPException as exc:
            return exc.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(save, ["a", "b"])) == [1, 409]


def test_whole_book_position_follows_ordinal_and_unknown_duration_is_nullable(
    tmp_path: Path,
) -> None:
    db, client = client_for(tmp_path)
    book = db.book("book-key")
    first = book["tracks"][0]
    second = {**first, "id": "second", "duration": 120, "name": "Second"}
    book["tracks"] = [first, second]
    book["duration"] = 180
    db.save_library([book], [first, second])
    db.save_progress("book-key", "second", 30)
    progress = client.get("/api/books/book-key").json()["progress"]
    assert progress["book_position"] == 90 and progress["fraction"] == 0.5
    assert [
        track["id"] for track in client.get("/api/v1/books/book-key/playback").json()["tracks"]
    ] == ["drive-track", "second"]
    second["duration"] = 0
    db.save_library([book], [first, second])
    assert db.book("book-key")["progress"]["fraction"] is None


def test_playlist_item_additions_are_atomic_and_replacement_is_conditional(tmp_path: Path) -> None:
    db, client = client_for(tmp_path)
    add_books(db, 3)
    playlist = client.post("/api/playlists", json={"name": "Queue"}).json()["id"]

    def add(book: str) -> int:
        return client.put(f"/api/playlists/{playlist}/books/{book}").status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(add, ["b0", "b1"])) == [200, 200]
    current = client.get("/api/features").json()["playlists"][0]
    assert set(current["book_ids"]) == {"b0", "b1"} and current["revision"] == 2
    assert client.put(f"/api/playlists/{playlist}/books/b0").json()["revision"] == 2
    path = f"/api/playlists/{playlist}/books"
    assert client.put(path, json={"book_ids": ["b2"]}).status_code == 428
    assert client.put(path, json={"book_ids": ["b2"], "base_revision": 1}).status_code == 409
    assert (
        client.put(path, json={"book_ids": ["b1", "b0"], "base_revision": 2}).json()["revision"]
        == 3
    )
    removed = client.delete(f"/api/playlists/{playlist}/books/b0").json()
    assert removed["book_ids"] == ["b1"] and removed["revision"] == 4
    book = db.book("b1")
    db.save_library([book], [db.track("t1")])
    assert client.get("/api/features").json()["playlists"][0]["book_ids"] == ["b1"]


def test_history_paging_storage_labels_bootstrap_and_json_404(tmp_path: Path) -> None:
    db, client = client_for(tmp_path)
    for position in range(5):
        assert (
            client.post(
                "/api/history/book-key", json={"track_id": "drive-track", "position": position}
            ).status_code
            == 204
        )
    page = client.get("/api/v1/history?limit=2").json()
    assert [item["position"] for item in page["items"]] == [4, 3]
    second = client.get(
        "/api/v1/history", params={"limit": 2, "cursor": page["next_cursor"]}
    ).json()
    assert [item["position"] for item in second["items"]] == [2, 1]
    storage = client.get("/api/storage").json()
    assert storage["source_bytes"] == storage["bytes"] == 16
    assert storage["cached_bytes"] == 0 and storage["cache_budget_bytes"] > 0
    bootstrap = client.get("/api/bootstrap")
    assert bootstrap.json()["books"][0]["id"] == "book-key"
    assert bootstrap.headers["cache-control"] == "private, no-store"
    missing = client.get("/api/typo")
    assert missing.status_code == 404 and missing.json()["code"] == "not_found"
    assert client.get("/library").status_code == 200
    add_books(db, 25)
    bounded = client.get("/api/v1/bootstrap").json()
    assert len(bounded["books"]["items"]) == 20 and bounded["books"]["next_cursor"]


def test_open_library_normalizes_author_description_and_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, client = client_for(tmp_path)

    class Provider:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> Provider:
            return self

        async def __aexit__(self, *args: Any) -> None:
            pass

        async def get(self, url: str) -> httpx.Response:
            value = (
                {"name": "An Author"}
                if "/authors/" in url
                else {
                    "title": "Work",
                    "description": {"value": "Text"},
                    "first_publish_date": "2000",
                    "authors": [{"author": {"key": "/authors/OL1A"}}],
                }
            )
            return httpx.Response(200, json=value, request=httpx.Request("GET", url))

    monkeypatch.setattr(web.httpx, "AsyncClient", Provider)
    result = client.post(
        "/api/books/book-key/metadata/apply", json={"source": "Open Library", "source_id": "OL1W"}
    )
    assert result.status_code == 200
    assert result.json()["artist"] == "An Author"
    assert result.json()["description"] == "Text" and result.json()["published_date"] == "2000"


def test_catalog_reads_do_not_start_scans_and_explicit_jobs_have_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db, client = client_for(tmp_path)
    scanner = client.app.state.scanner
    started = []

    def start(**kwargs: Any) -> bool:
        started.append(kwargs)
        db.save_scan_status(status="running", processed=0)
        return True

    monkeypatch.setattr(scanner, "start", start)
    db.save_library([], [])
    assert client.get("/api/library").json() == []
    assert started == []
    assert client.get("/api/library?refresh=true").status_code == 405
    result = client.post("/api/v1/scan-jobs")
    assert result.status_code == 202 and result.json()["job_id"]
    first = result.json()["job_id"]
    db.save_scan_status(status="completed")
    db.save_scan_status(status="running")
    assert db.scan_status()["job_id"] != first


def test_bootstrap_does_not_repeat_feature_queries_and_cache_disabled_is_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(tmp_path)
    cfg.cache_warm_enabled = False
    db = seeded_db(cfg.db_path)
    client = TestClient(create_app(cfg, db))
    sign_in(client)
    original = db.feature_data
    calls = []

    def features(*args: Any) -> dict[str, Any]:
        calls.append(True)
        return original(*args)

    monkeypatch.setattr(db, "feature_data", features)
    assert client.get("/api/bootstrap").status_code == 200
    assert len(calls) == 1
    response = client.post("/api/tracks/drive-track/warm")
    assert response.status_code == 200 and response.json() == {"status": "disabled"}


def test_exclusions_reorder_surviving_tracks_and_keep_status_filters_consistent(
    tmp_path: Path,
) -> None:
    db, client = client_for(tmp_path)
    book = db.book("book-key")
    first = {**book["tracks"][0], "path": "Book/Disc 1/part.mp3"}
    second = {**first, "id": "second", "path": "Book/Disc 2/part.mp3"}
    book["tracks"] = [first, second]
    book["duration"] = 120
    db.save_library([book], [first, second])
    db.save_progress("book-key", "second", 0)
    db.replace_excluded_directories([{"path": "Book/Disc 1", "name": "Disc 1"}])
    surviving = db.book("book-key")
    assert surviving["tracks"][0]["ordinal"] == 0
    assert surviving["track_count"] == 1
    assert surviving["progress"]["status"] == "not-started"
    assert client.get("/api/v1/books?status=not-started").json()["total"] == 1
