from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect

import audioscan.web as web
from audioscan.models import TrackMeta
from audioscan.naming import group_tracks
from audioscan.reader import FetchError
from audioscan.web import Database, LibraryScanner, MediaCache, StoredDriveAuth, create_app
from test_web import config, seeded_db, sign_in


def scan_item(index: int, directory: str = "Book") -> dict[str, Any]:
    meta = TrackMeta(
        id=f"track-{index}",
        name=f"{index:03}.mp3",
        path=f"{directory}/{index:03}.mp3",
        album=directory,
        artist="Author",
        duration=60,
    )
    return {
        "id": meta.id,
        "name": meta.name,
        "path": meta.path,
        "size": 16,
        "mime_type": "audio/mpeg",
        "modified": None,
        "md5": None,
        "meta": meta.to_dict(),
        "error": "",
    }


def test_interrupted_scan_keeps_live_library_and_resumes_atomically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = config(tmp_path)
    db = Database(cfg.db_path)
    scanner = LibraryScanner(cfg, db)
    items = [scan_item(i, "Early" if i < 50 else "Late") for i in range(51)]
    scanner._commit(items)
    late = next(book for book in db.library() if book["title"] == "Late")
    db.update_book_metadata(late["id"], {"title": "Manual late title"})
    original = db.library()
    remote = [
        web.RemoteFile(
            **{
                key: item[key]
                for key in (
                    "id",
                    "name",
                    "path",
                    "size",
                    "mime_type",
                    "modified",
                    "md5",
                )
            }
        )
        for item in items
    ]

    class Source:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def iter_child_directories(self, *args: Any) -> list[Any]:
            return []

        def iter_directory_files(self, *args: Any) -> list[Any]:
            return remote

        def close(self) -> None:
            pass

    monkeypatch.setattr(web, "DriveSource", Source)
    by_id = {item["id"]: item for item in items}

    def interrupted(source: Any, file: Any) -> dict[str, Any]:
        if file.id == "track-50":
            raise RuntimeError("interrupted scan")
        return by_id[file.id]

    monkeypatch.setattr(scanner, "_probe_remote", interrupted)
    with pytest.raises(RuntimeError, match="interrupted"):
        scanner._run(retry_failed=True, resume=False)
    assert db.library() == original
    assert db.pending_scan_items()  # Durable checkpoints exist without publication.
    monkeypatch.setattr(scanner, "_probe_remote", lambda source, file: by_id[file.id])
    scanner._run(retry_failed=True, resume=True)
    assert len(db.library()) == 2
    assert db.book(late["id"])["title"] == "Manual late title"
    assert len(db.identities()) == 51


def test_stable_identity_preserves_personal_state_after_rename(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    db = Database(cfg.db_path)
    scanner = LibraryScanner(cfg, db)
    scanner._commit([scan_item(1)])
    original = db.library()[0]["id"]
    db.update_book_metadata(original, {"title": "Personal title"})
    db.save_progress(original, "track-1", 12)
    with db.connect() as conn:
        conn.execute("INSERT INTO favorites VALUES(?)", (original,))
    scanner._commit([scan_item(1, "Renamed")])
    book = db.library()[0]
    assert book["id"] == original
    assert book["title"] == "Personal title"
    assert book["progress"]["position"] == 12
    assert book["favorite"] is True


def test_grouping_preserves_unicode_punctuation_and_edition_directories() -> None:
    tracks = [
        TrackMeta(id=str(i), name="1.mp3", path=f"{name}/1.mp3")
        for i, name in enumerate(["日本語", "中文", "A-B", "A B"])
    ]
    assert len(group_tracks(tracks)) == 4
    for track in tracks:
        track.album = "Same title"
        track.artist = "Same author"
    assert len(group_tracks(tracks)) == 4
    assert all(group.key for group in group_tracks(tracks))


def test_legacy_database_migration_retains_ids_and_overrides(tmp_path: Path) -> None:
    db = seeded_db(tmp_path / "library.sqlite3")
    db.update_book_metadata("book-key", {"title": "Legacy title"})
    with db.connect() as conn:
        conn.execute("DROP TABLE book_overrides")
        conn.execute("DROP TABLE track_identity")
    db = Database(db.path)
    assert db.identities()["drive-track"] == "book-key"
    scanner = LibraryScanner(config(tmp_path), db)
    item = scan_item(1)
    item["id"] = item["meta"]["id"] = "drive-track"
    scanner._commit([item])
    assert db.book("book-key")["title"] == "Legacy title"


def test_logout_revokes_saved_cookie_for_cached_audio_and_writes(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    db = seeded_db(cfg.db_path)
    track = db.track("drive-track")
    cache = MediaCache(cfg.cache_path, cfg.cache_max_bytes)
    cache.put(track, iter([b"0123456789abcdef"]))
    client = TestClient(create_app(cfg, db))
    sign_in(client)
    cookie = client.cookies.get("session")
    assert client.get("/api/tracks/drive-track/audio").status_code == 200
    assert client.post("/auth/logout").status_code == 204
    client.cookies.clear()
    client.cookies.set("session", cookie)
    assert client.get("/api/books/book-key").status_code == 401
    assert client.get("/api/tracks/drive-track/audio").status_code == 401
    assert client.put("/api/progress/book-key", json={"track_id": "drive-track"}).status_code == 401


def test_logout_fences_loaded_credentials_and_stale_persistence(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    db = seeded_db(cfg.db_path)
    auth = StoredDriveAuth(db, cfg.credentials_path)
    auth.creds = SimpleNamespace(valid=True, token="synthetic")
    generation = auth.generation
    db.disconnect(cfg.credentials_path)
    with pytest.raises(web.DriveError, match="session ended"):
        auth.headers()
    assert not db.save_credentials(json.dumps({"refresh_token": "fake"}), generation)
    db.sync_credentials(cfg.credentials_path)
    assert db.credentials() is None
    assert not cfg.credentials_path.exists()


def test_logout_during_refresh_cannot_restore_credentials(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    db = seeded_db(cfg.db_path)
    auth = StoredDriveAuth(db, cfg.credentials_path)

    class Credentials:
        valid = False
        token = "synthetic"

        def refresh(self, request: Any) -> None:
            # Simulate disconnect after the initial generation check.
            db.disconnect(cfg.credentials_path)

        def to_json(self) -> str:
            return json.dumps({"refresh_token": "fake"})

    auth.creds = Credentials()
    with pytest.raises(web.DriveError, match="session ended"):
        auth.headers()
    assert db.credentials() is None
    assert not cfg.credentials_path.exists()


def test_scan_reconciliation_cleans_removed_track_and_book_state(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    db = Database(cfg.db_path)
    scanner = LibraryScanner(cfg, db)
    first, second = scan_item(1), scan_item(2)
    scanner._commit([first, second])
    book = db.library()[0]["id"]
    db.save_progress(book, "track-2", 10)
    db.record_history(book, "track-2")
    scanner._commit([first])
    assert db.book(book)["progress"] is None
    assert db.feature_data()["history"] == []
    with db.connect() as conn:
        conn.execute("INSERT INTO favorites VALUES(?)", (book,))
        conn.execute("INSERT INTO ratings VALUES(?,5)", (book,))
        conn.execute("INSERT INTO book_tags VALUES(?,'tag')", (book,))
        conn.execute("INSERT INTO playlists(id,name) VALUES('p','Playlist')")
        conn.execute("INSERT INTO playlist_books VALUES('p',?,0)", (book,))
    scanner._commit([])
    features = db.feature_data()
    assert features["favorites"] == []
    assert features["ratings"] == {}
    assert features["tags"] == {}
    assert features["playlists"][0]["book_ids"] == []


def test_transient_probe_result_retries_but_corrupt_media_does_not(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = config(tmp_path)
    scanner = LibraryScanner(cfg, Database(cfg.db_path))
    calls: list[int] = []
    source = SimpleNamespace(open=lambda remote: SimpleNamespace(close=lambda: None))
    remote = web.RemoteFile(id="x", name="x.mp3", path="x.mp3", size=16)

    def transient(*args: Any, **kwargs: Any) -> TrackMeta:
        calls.append(1)
        return (
            TrackMeta(error="temporary", retryable_error=True) if len(calls) == 1 else TrackMeta()
        )

    monkeypatch.setattr(web, "probe", transient)
    monkeypatch.setattr(web.time, "sleep", lambda delay: None)
    assert scanner._probe_remote(source, remote)["error"] == ""
    assert len(calls) == 2
    calls.clear()

    def corrupt(*args: Any, **kwargs: Any) -> TrackMeta:
        calls.append(1)
        return TrackMeta(error="unsupported format")

    monkeypatch.setattr(web, "probe", corrupt)
    assert scanner._probe_remote(source, remote)["error"]
    assert len(calls) == 1


def test_probe_marks_transport_errors_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    from importlib import import_module

    module = import_module("audioscan.probe")

    def fail(*args: Any) -> None:
        raise FetchError("transport failed")

    monkeypatch.setattr(module.mutagen, "File", fail)
    assert module.probe(SimpleNamespace(seek=lambda pos: None)).retryable_error is True


def test_cache_rejects_wrong_checksum_and_open_stream_survives_eviction(tmp_path: Path) -> None:
    cache = MediaCache(tmp_path / "cache", 4)
    track = {
        "id": "one",
        "name": "one.mp3",
        "size": 4,
        "md5": hashlib.md5(b"good", usedforsecurity=False).hexdigest(),
    }
    assert cache.put(track, iter([b"evil"])) is None
    assert cache.file(track) is None
    path = cache.put(track, iter([b"good"]))
    assert path is not None
    stream = cache.serve(path, 1, 3)
    cache.put({"id": "two", "name": "two.mp3", "size": 4}, iter([b"next"]))
    assert not path.exists()
    assert b"".join(stream) == b"ood"


@pytest.mark.parametrize("body", [[], None, 4, {}, {"rating": True}, {"rating": "4"}])
def test_invalid_rating_input_never_mutates(tmp_path: Path, body: Any) -> None:
    cfg = config(tmp_path)
    db = seeded_db(cfg.db_path)
    client = TestClient(create_app(cfg, db))
    sign_in(client)
    assert client.put("/api/books/book-key/rating", json={"rating": 4}).status_code == 200
    assert (
        client.put(
            "/api/books/book-key/rating",
            content=json.dumps(body),
            headers={"Content-Type": "application/json"},
        ).status_code
        == 422
    )
    assert db.feature_data()["ratings"]["book-key"] == 4
    assert client.put("/api/books/book-key/rating", json={"rating": None}).status_code == 200


def test_malformed_json_is_a_client_error(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    client = TestClient(create_app(cfg, seeded_db(cfg.db_path)))
    sign_in(client)
    response = client.put(
        "/api/books/book-key/favorite", content="{", headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422


def test_progress_contention_does_not_block_other_requests(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    db = seeded_db(cfg.db_path)
    app = create_app(cfg, db)
    client = TestClient(app)
    sign_in(client)
    client.get("/api/books/book-key")  # Perform scan recovery before taking the write lock.
    cookie = client.cookies.get("session")
    locked = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            locked.set()
            release.wait(5)

    worker = threading.Thread(target=hold)
    worker.start()
    assert locked.wait(2)

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            cookies={"session": cookie},
        ) as ac:
            pending = asyncio.create_task(
                ac.put("/api/progress/book-key", json={"track_id": "drive-track", "position": 12, "base_revision": 0, "event_id": "heartbeat"})
            )
            try:
                response = await asyncio.wait_for(ac.get("/api/books/book-key"), timeout=1)
                assert response.status_code == 200
            finally:
                release.set()
            assert (await pending).status_code == 200

    try:
        asyncio.run(run())
    finally:
        release.set()
        worker.join(2)


def test_disc_folders_group_within_their_edition() -> None:
    tracks = [
        TrackMeta(id=str(i), name="1.mp3", album="Book", artist="Author", path=path)
        for i, path in enumerate(
            ["Edition/Disc 1/1.mp3", "Edition/CD 2/1.mp3", "Other edition/Disc 1/1.mp3"]
        )
    ]
    groups = group_tracks(tracks)
    assert sorted(group.count for group in groups) == [1, 2]


def test_failed_existing_probe_preserves_successful_metadata(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    db = Database(cfg.db_path)
    scanner = LibraryScanner(cfg, db)
    item = scan_item(1)
    item["meta"]["chapters"] = [{"number": 1, "start": 0, "title": "Opening"}]
    scanner._commit([item])
    original = db.library()[0]["id"]
    failed = scan_item(1)
    failed["meta"] = TrackMeta(
        id="track-1",
        name="001.mp3",
        path="Book/001.mp3",
        error="transport failed",
        retryable_error=True,
    ).to_dict()
    failed["error"] = "transport failed"
    scanner._commit([failed])
    book = db.book(original)
    assert book["duration"] == 60
    assert book["artist"] == "Author"
    assert book["tracks"][0]["chapters"][0]["title"] == "Opening"
    assert "probe_meta" not in book["tracks"][0]


def test_empty_legacy_book_id_is_repaired_with_personal_state(tmp_path: Path) -> None:
    db = seeded_db(tmp_path / "library.sqlite3")
    with db.connect() as conn:
        book = json.loads(conn.execute("SELECT data FROM books").fetchone()[0])
        book["id"] = ""
        book["tracks"][0]["book_id"] = ""
        conn.execute("UPDATE books SET id='',data=?", (json.dumps(book),))
        track = json.loads(conn.execute("SELECT data FROM tracks").fetchone()[0])
        track["book_id"] = ""
        conn.execute("UPDATE tracks SET book_id='',data=?", (json.dumps(track),))
        conn.execute("UPDATE track_identity SET book_id=''")
        conn.execute("INSERT INTO favorites VALUES('')")
    db = Database(db.path)
    book = db.library()[0]
    assert book["id"].startswith("book-")
    assert book["favorite"] is True
    assert db.identities()["drive-track"] == book["id"]
    assert db.track("drive-track")["book_id"] == book["id"]


def test_cached_response_closes_handle_if_sending_fails(tmp_path: Path) -> None:
    cache = MediaCache(tmp_path / "cache", 16)
    track = {"id": "one", "name": "one.mp3", "size": 4}
    path = cache.put(track, iter([b"data"]))
    assert path is not None
    stream = cache.serve(path, 0, 4)
    response = web.CachedResponse(stream)

    async def receive() -> dict[str, Any]:
        return {"type": "http.request"}

    async def send(message: dict[str, Any]) -> None:
        raise OSError("disconnected before first read")

    with pytest.raises(ClientDisconnect):
        asyncio.run(response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send))
    assert stream.source.closed
