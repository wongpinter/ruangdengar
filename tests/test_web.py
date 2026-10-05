from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from audioscan.reader import BytesFetcher, Fetcher
from audioscan.web import Database, WebConfig, _range_header, create_app


def config(tmp_path: Path) -> WebConfig:
    result = WebConfig()
    result.allowed_email = "reader@example.com"
    result.base_url = "https://books.example.com"
    result.secret = "local-test-key-" + "x" * 32
    result.client_secrets = tmp_path / "client.json"
    result.folder_id = "folder-id"
    result.db_path = tmp_path / "library.sqlite3"
    result.credentials_path = tmp_path / "credentials.json"
    result.cache_path = tmp_path / "media-cache"
    result.cache_max_bytes = 1024
    return result


def test_deployment_settings_require_https_long_secret_and_root_folder(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    cfg.base_url = "http://books.example.com"
    cfg.cookie_secure = False
    cfg.host = "books.example.com"
    cfg.require_https = False
    with pytest.raises(RuntimeError, match="HTTPS"):
        cfg.check()
    cfg.base_url = "https://books.example.com"
    cfg.cookie_secure = True
    cfg.require_https = True
    cfg.secret = "short"
    with pytest.raises(RuntimeError, match="32 characters"):
        cfg.check()
    cfg.secret = "strong-secret-" + "x" * 32
    cfg.folder_id = ""
    with pytest.raises(RuntimeError, match="AUDIOBOOKS_FOLDER_ID"):
        cfg.check()


def test_media_cache_uses_md5_and_evicts_oldest(tmp_path: Path) -> None:
    from audioscan.web import MediaCache

    cache = MediaCache(tmp_path / "cache", max_bytes=8)
    first = {"id": "one", "name": "one.m4b", "md5": "827ccb0eea8a706c4c34a16891f84e7b", "modified": "today", "size": 5}
    second = {"id": "two", "name": "two.mp3", "md5": "ab56b4d92b40713acc5af89985d4b786", "modified": "today", "size": 5}
    path = cache.put(first, iter([b"12345"]))
    assert path is not None and cache.file(first) == path
    assert next(cache.serve(path, 1, 3)) == b"234"
    cache.put(second, iter([b"abcde"]))
    assert cache.file(second) is not None
    assert cache.file(first) is None
    assert cache.file({**second, "md5": "xyz"}) is None
    partial = {**second, "id": "partial", "md5": "partial"}
    assert cache.put(partial, iter([b"abc"])) is None
    assert cache.file(partial) is None


def test_range_header_supports_byte_and_suffix_ranges() -> None:
    assert _range_header("bytes=10-19", 100) == (10, 19)
    assert _range_header("bytes=90-", 100) == (90, 99)
    assert _range_header("bytes=-10", 100) == (90, 99)
    assert _range_header("bytes=-200", 100) == (0, 99)
    for value in ("bytes=100-", "bytes=10-9", "bytes=0-1,4-5", "items=0-2"):
        with pytest.raises(ValueError):
            _range_header(value, 100)


def test_range_headers_reject_empty_suffix_and_huge_offset() -> None:
    for value in ("bytes=-0", "bytes=", "bytes=999999999999999999999999999-"):
        with pytest.raises(ValueError):
            _range_header(value, 100)


def seeded_db(path: Path) -> Database:
    db = Database(path)
    track = {
        "id": "drive-track",
        "book_id": "book-key",
        "name": "Book.m4b",
        "path": "Book.m4b",
        "size": 16,
        "mime_type": "application/octet-stream",
        "title": "Book",
        "duration": 60,
        "chapters": [],
    }
    book = {
        "id": "book-key",
        "title": "Book",
        "artist": "Author",
        "cover": None,
        "duration": 60,
        "tracks": [track],
    }
    db.save_library([book], [track])
    return db


def sign_in(client: TestClient) -> None:
    from itsdangerous import TimestampSigner
    from starlette.middleware.sessions import SessionMiddleware

    middleware = next(m for m in client.app.user_middleware if m.cls is SessionMiddleware)
    signer = TimestampSigner(middleware.kwargs["secret_key"])
    from base64 import b64encode

    cookie = signer.sign(b64encode(json.dumps({"email": "reader@example.com", "generation": client.app.state.db.auth_generation()}).encode())).decode()
    client.cookies.set("session", cookie)


def test_excluded_directory_settings_use_scanned_paths_without_drive_listing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import audioscan.web as web

    cfg = config(tmp_path)
    db = seeded_db(cfg.db_path)

    class NoDriveListing:
        def __init__(self, **kwargs: Any) -> None:
            raise AssertionError("Settings must not call Drive")

    monkeypatch.setattr(web, "DriveSource", NoDriveListing)
    client = TestClient(create_app(cfg, db))
    sign_in(client)
    assert client.get("/api/directories").json() == []
    assert client.get("/api/settings/excluded-directories").json() == {"excluded_directories": []}
    book = db.book("book-key")
    assert book is not None
    book["tracks"][0]["path"] = "Movies/Series/Book.m4b"
    db.save_library([book], [book["tracks"][0]])
    assert client.get("/api/directories").json() == [
        {"id": "Movies", "name": "Movies", "path": "Movies"},
        {"id": "Movies/Series", "name": "Series", "path": "Movies/Series"},
    ]
    assert (
        client.put("/api/settings/excluded-directories", json={"paths": ["Movies"]}).status_code
        == 200
    )
    assert db.library() == []


def test_excluded_directory_removes_matching_media_and_book_features(tmp_path: Path) -> None:
    db = seeded_db(tmp_path / "library.sqlite3")
    book = db.book("book-key")
    assert book is not None
    book["tracks"][0]["path"] = "Movies/Book.m4b"
    db.save_library([book], [book["tracks"][0]])
    db.save_progress("book-key", "drive-track", 20)
    with db.connect() as connection:
        connection.execute("INSERT INTO favorites(book_id) VALUES(?)", ("book-key",))
        connection.execute("INSERT INTO ratings(book_id,rating) VALUES(?,?)", ("book-key", 5))
        connection.execute("INSERT INTO book_tags(book_id,tag) VALUES(?,?)", ("book-key", "Sci-fi"))
        connection.execute("INSERT INTO playlists(id,name) VALUES(?,?)", ("playlist", "Books"))
        connection.execute(
            "INSERT INTO playlist_books(playlist_id,book_id,position) VALUES(?,?,?)",
            ("playlist", "book-key", 0),
        )
    db.replace_excluded_directories([{"id": "movies-id", "name": "Movies", "path": "Movies"}])
    assert db.library() == []
    with db.connect() as connection:
        for table in ("tracks", "progress", "favorites", "ratings", "book_tags", "playlist_books"):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert db.excluded_directories() == [{"id": "Movies", "name": "Movies", "path": "Movies"}]


def test_library_features_favorites_ratings_tags_playlists_history_storage(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    db = seeded_db(cfg.db_path)
    client = TestClient(create_app(cfg, db))
    sign_in(client)

    assert client.put("/api/books/book-key/favorite", json={"favorite": True}).json() == {
        "favorite": True
    }
    assert client.put("/api/books/book-key/rating", json={"rating": 5}).json() == {"rating": 5}
    assert client.put(
        "/api/books/book-key/tags", json={"tags": ["Sci-fi", "  Classic "]}
    ).json() == {"tags": ["Sci-fi", "Classic"]}
    playlist = client.post("/api/playlists", json={"name": "Road trip"})
    assert playlist.status_code == 201
    playlist_id = playlist.json()["id"]
    assert client.put(
        f"/api/playlists/{playlist_id}/books", json={"book_ids": ["book-key"], "base_revision": 0}
    ).json() == {"book_ids": ["book-key"], "revision": 1}
    assert client.post("/api/history/book-key", json={"track_id": "drive-track"}).status_code == 204
    assert client.get("/api/features").json()["playlists"][0]["book_ids"] == ["book-key"]
    book = client.get("/api/library").json()[0]
    assert client.get("/manifest.webmanifest").json()["short_name"] == "RuangDengar"
    assert (
        book["favorite"] is True and book["rating"] == 5 and book["tags"] == ["Classic", "Sci-fi"]
    )
    assert client.get("/api/features").json()["history"][0]["track_id"] == "drive-track"
    assert client.get("/api/storage").json()["bytes"] == 16

    assert client.put("/api/books/book-key/rating", json={"rating": 6}).status_code == 422
    assert client.put("/api/books/book-key/tags", json={"tags": [""]}).status_code == 422
    assert (
        client.put(
            f"/api/playlists/{playlist_id}/books", json={"book_ids": ["missing"]}
        ).status_code
        == 422
    )
    assert client.delete(f"/api/playlists/{playlist_id}").status_code == 204
    assert client.put("/api/books/book-key/favorite", json={"favorite": False}).json() == {
        "favorite": False
    }


def test_chapter_endpoint_pages_results_and_validates_bounds(tmp_path: Path) -> None:
    db = seeded_db(tmp_path / "app.sqlite")
    book = db.book("book-key")
    assert book is not None
    chapters = [{"number": i, "start": float(i), "title": f"Chapter {i}"} for i in range(1, 4)]
    book["tracks"][0]["chapters"] = chapters
    db.save_library([book], [book["tracks"][0]])
    client = TestClient(create_app(config(tmp_path), db))
    sign_in(client)

    first = client.get("/api/tracks/drive-track/chapters?offset=0&limit=2")
    assert first.status_code == 200
    assert [item["number"] for item in first.json()["chapters"]] == [1, 2]
    assert first.json()["next_offset"] == 2
    last = client.get("/api/tracks/drive-track/chapters?offset=2&limit=2")
    assert [item["number"] for item in last.json()["chapters"]] == [3]
    assert last.json()["next_offset"] is None
    assert client.get("/api/tracks/drive-track/chapters?offset=-1").status_code == 422
    assert client.get("/api/tracks/drive-track/chapters?limit=101").status_code == 422


def test_private_routes_require_allowed_signed_in_user(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    client = TestClient(create_app(cfg, seeded_db(cfg.db_path)))
    assert client.get("/api/library").status_code == 401
    sign_in(client)
    response = client.get("/api/library")
    assert response.status_code == 200
    assert response.json()[0]["title"] == "Book"


def test_invalid_app_settings_block_library_scan(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    cfg.folder_id = ""
    client = TestClient(create_app(cfg, seeded_db(cfg.db_path)))
    sign_in(client)
    response = client.get("/api/library?refresh=true")
    assert response.status_code == 503
    assert "AUDIOBOOKS_FOLDER_ID" in response.text


def test_progress_requires_valid_book_track_and_position(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    db = seeded_db(cfg.db_path)
    client = TestClient(create_app(cfg, db))
    sign_in(client)
    path = "/api/progress/book-key"
    assert client.put(path, json={"track_id": "wrong", "position": 10, "base_revision": 0, "event_id": "wrong"}).status_code == 422
    assert client.put(path, json={"track_id": "drive-track", "position": -1}).status_code == 422
    saved = client.put(path, json={"track_id": "drive-track", "position": 12.5, "base_revision": 0, "event_id": "first"})
    assert saved.status_code == 200
    assert db.library()[0]["progress"]["position"] == 12.5


def test_audio_cache_hit_serves_ranges_without_drive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from audioscan.web import MediaCache

    cfg = config(tmp_path)
    db = seeded_db(cfg.db_path)
    cache = MediaCache(cfg.cache_path, cfg.cache_max_bytes)
    track = db.track("drive-track")
    assert track is not None
    cache.put(track, iter([b"0123456789abcdef"]))

    def fail_if_drive(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Drive must not be called on a cache hit")

    import audioscan.web as web
    monkeypatch.setattr(web.HttpRangeFetcher, "open", fail_if_drive)
    client = TestClient(create_app(cfg, db))
    sign_in(client)
    response = client.get("/api/tracks/drive-track/audio", headers={"Range": "bytes=4-7"})
    assert response.status_code == 206
    assert response.content == b"4567"
    assert response.headers["content-range"] == "bytes 4-7/16"


def test_audio_endpoint_proxies_only_requested_range(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import audioscan.web as web

    original_spool = web.tempfile.TemporaryFile
    writes: list[bytes] = []

    class DiskSpool:
        def __init__(self, **kwargs: Any) -> None:
            self.file = original_spool(**kwargs)

        def __enter__(self) -> Any:
            return self

        def __exit__(self, *args: Any) -> None:
            self.file.close()

        def write(self, chunk: bytes) -> int:
            writes.append(chunk)
            return self.file.write(chunk)

        def seek(self, offset: int) -> int:
            return self.file.seek(offset)

        def read(self, size: int) -> bytes:
            return self.file.read(size)

    monkeypatch.setattr(web.tempfile, "TemporaryFile", DiskSpool)

    cfg = config(tmp_path)
    db = seeded_db(cfg.db_path)
    payload = b"0123456789abcdef"

    def open_reader(*args: Any, **kwargs: Any) -> Fetcher:
        return BytesFetcher(payload)

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(web.HttpRangeFetcher, "open", open_reader)
    monkeypatch.setattr(web.httpx, "Client", FakeClient)
    client = TestClient(create_app(cfg, db))
    sign_in(client)
    response = client.get("/api/tracks/drive-track/audio")
    assert response.status_code == 200
    assert response.content == payload
    assert b"".join(writes) == payload
    assert len(list((tmp_path / "media-cache").glob("*"))) == 1
    assert response.headers["content-type"].startswith("audio/mp4")


def test_playback_metrics_require_auth_and_validate_values(tmp_path: Path) -> None:
    db = seeded_db(tmp_path / "library.sqlite3")
    client = TestClient(create_app(config(tmp_path), db))
    metrics = {
        "startupMs": 500,
        "stalls": 1,
        "bufferedAhead": 20,
        "rangeMs": 700,
        "rangeBytes": 65536,
        "ranges": 2,
    }
    assert client.post("/api/tracks/drive-track/playback-metrics", json=metrics).status_code == 401
    sign_in(client)
    assert client.post("/api/tracks/drive-track/playback-metrics", json=metrics).status_code == 204
    assert (
        client.post(
            "/api/tracks/drive-track/playback-metrics", json={**metrics, "stalls": -1}
        ).status_code
        == 422
    )
    assert client.post("/api/tracks/missing/playback-metrics", json=metrics).status_code == 404


def test_scan_commit_restores_nested_metadata_types(tmp_path: Path) -> None:
    from audioscan.models import Chapter, Cover
    from audioscan.web import LibraryScanner

    cfg = config(tmp_path)
    db = seeded_db(cfg.db_path)
    scanner = LibraryScanner(cfg, db)
    item = {
        "id": "track-1",
        "path": "Book/track.mp3",
        "name": "track.mp3",
        "size": 100,
        "mime_type": "audio/mpeg",
        "error": "",
        "meta": {
            "id": "track-1",
            "name": "track.mp3",
            "path": "Book/track.mp3",
            "album": "Book",
            "artist": "Narrator",
            "albumartist": "Collection Author",
            "covers": [Cover(index=0, mime="image/jpeg").to_dict()],
            "chapters": [Chapter(number=1, start=0, title="Start").to_dict()],
        },
    }
    scanner._commit([item])
    book = db.book(db.library()[0]["id"])
    assert book is not None
    assert book["tracks"][0]["title"] == "track.mp3"
    assert book["artist"] == "Narrator"
    assert book["album_artist"] == "Collection Author"


def test_scan_status_partial_update_keeps_required_status(tmp_path: Path) -> None:
    db = seeded_db(tmp_path / "library.sqlite3")
    db.save_scan_status(status="running", total=0, processed=0)
    db.save_scan_status(total=3, current="Book/")
    status = db.scan_status()
    assert status is not None
    assert status["status"] == "running"
    assert status["total"] == 3
    assert status["current"] == "Book/"


def test_scan_resume_uses_persisted_directory_queue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import audioscan.web as web
    from audioscan.web import LibraryScanner

    cfg = config(tmp_path)
    db = seeded_db(cfg.db_path)
    db.enqueue_scan_directory("child", "Child")
    db.queue_scan_directories([("grandchild", "Child/Grandchild/")])
    db.mark_scan_directory("child", listed=True)
    db.mark_scan_directory("child", scanned=True)
    file = web.RemoteFile(id="missing", name="missing.mp3", path="Child/missing.mp3")
    db.save_scan_files("child", [file])
    db.mark_scan_files_listed("grandchild")
    db.mark_scan_directory("grandchild", listed=True, scanned=True)
    calls: list[tuple[str, str]] = []

    class Source:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def iter_child_directories(self, folder_id: str, prefix: str) -> list[tuple[str, str]]:
            calls.append(("folders", prefix))
            return [("grandchild", f"{prefix.rstrip('/')}/Grandchild/")] if folder_id == "child" else []

        def iter_directory_files(self, folder_id: str, prefix: str) -> list[Any]:
            calls.append(("files", prefix))
            return [file] if folder_id == "child" else []

        def close(self) -> None:
            pass

    monkeypatch.setattr(web, "DriveSource", Source)
    scanner = LibraryScanner(cfg, db)
    monkeypatch.setattr(scanner, "_commit", lambda items: None)
    monkeypatch.setattr(
        scanner,
        "_probe_remote",
        lambda source, remote: {
            "id": remote.id, "path": remote.path, "name": remote.name,
            "meta": {}, "error": "bad media metadata",
        },
    )
    scanner._run(retry_failed=False, resume=True)

    assert calls == []
    assert db.pending_scan_items()[0]["id"] == "missing"
    assert db.scan_file_count() == 1
    status = db.scan_status()
    assert status is not None
    assert status["status"] == "completed"
    assert status["processed"] == status["total"] == 1
    assert "1 file(s) failed" in status["error"]
    assert db.scan_directories(listed=False) == []
    assert db.scan_directories(scanned=False) == []
    assert [item[:2] for item in db.inventory_directories()] == [
        ("child", "Child"),
        ("grandchild", "Child/Grandchild/"),
    ]
    assert all(item[4] for item in db.inventory_directories())


def test_cover_route_returns_embedded_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import audioscan.web as web

    cfg = config(tmp_path)
    db = seeded_db(cfg.db_path)
    client = TestClient(create_app(cfg, db))
    sign_in(client)

    class Reader:
        def close(self) -> None:
            pass

    class Source:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def open(self, item: Any) -> Reader:
            return Reader()

        def close(self) -> None:
            pass

    monkeypatch.setattr(web, "DriveSource", Source)
    import importlib

    probe_module = importlib.import_module("audioscan.probe")
    monkeypatch.setattr(probe_module, "cover_bytes", lambda stream: (b"jpeg", "image/jpeg"))
    response = client.get("/api/tracks/drive-track/cover")
    assert response.status_code == 200
    assert response.content == b"jpeg"
    assert response.headers["content-type"].startswith("image/jpeg")


def test_library_scan_routes_return_progress_and_require_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import audioscan.web as web

    class IdleScanner:
        lock = web.threading.Lock()
        event_lock = web.threading.Lock()
        events: list[dict[str, Any]] = []
        event_id = 0
        db: Database

        def __init__(self, config: WebConfig, db: Database) -> None:
            self.db = db

        def stop_incomplete_scan(self) -> None:
            return None

        def start(self, retry_failed: bool = True) -> bool:
            if self.lock.locked():
                return False
            self.lock.acquire()
            self.db.save_scan_status(status="running", total=0, processed=0)
            return True

    cfg = config(tmp_path)
    client = TestClient(create_app(cfg, seeded_db(cfg.db_path)))
    assert client.post("/api/library/refresh").status_code == 401
    sign_in(client)
    monkeypatch.setattr(web, "LibraryScanner", IdleScanner)
    client = TestClient(create_app(cfg, seeded_db(cfg.db_path)))
    sign_in(client)
    assert client.get("/api/library/scan").json()["status"] == "idle"
    events = client.get("/api/library/events")
    assert events.status_code == 200
    assert "text/event-stream" in events.headers["content-type"]
    assert '"status": "idle"' in events.text
    assert client.post("/api/library/refresh").json()["status"] == "running"
    assert client.get("/api/library/scan").json()["status"] == "running"


def test_oauth_flow_preserves_pkce_verifier_across_callback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(tmp_path)
    cfg.client_secrets.write_text(
        json.dumps(
            {
                "web": {
                    "client_id": "client-id",
                    "client_secret": "client-secret",
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": ["https://books.example.com/auth/callback"],
                }
            }
        )
    )
    verifier = "test-verifier"

    class FakeFlow:
        credentials: Any
        client_config = {"client_id": "client-id"}
        redirect_uri = ""
        code_verifier = verifier
        oauth2session = SimpleNamespace(_client=SimpleNamespace(code_verifier=None))

        @classmethod
        def from_client_secrets_file(cls, *args: Any, **kwargs: Any) -> FakeFlow:
            result = cls()
            result.code_verifier = kwargs.get("code_verifier")
            return result

        def authorization_url(self, **kwargs: Any) -> tuple[str, str]:
            return (
                f"https://accounts.example.com/authorize?state={kwargs['state']}&code_challenge=test",
                kwargs["state"],
            )

        def fetch_token(self, **kwargs: Any) -> None:
            assert self.code_verifier
            assert self.oauth2session._client.code_verifier == self.code_verifier
            assert self.oauth2session._client.code_challenge is None

        @property
        def credentials(self) -> Any:
            return SimpleNamespace(
                id_token="identity-token",
                scopes=["https://www.googleapis.com/auth/drive.readonly"],
                refresh_token="refresh-token",
                to_json=lambda: json.dumps({"refresh_token": "refresh-token"}),
            )

    google_auth_oauthlib = SimpleNamespace(flow=SimpleNamespace(Flow=FakeFlow))
    monkeypatch.setitem(sys.modules, "google_auth_oauthlib", google_auth_oauthlib)
    monkeypatch.setitem(sys.modules, "google_auth_oauthlib.flow", google_auth_oauthlib.flow)
    client = TestClient(create_app(cfg, seeded_db(cfg.db_path)))
    from urllib.parse import parse_qs, urlsplit

    from google.oauth2 import id_token

    monkeypatch.setattr(
        id_token,
        "verify_oauth2_token",
        lambda *args, **kwargs: {"email": "reader@example.com", "email_verified": True},
    )
    monkeypatch.setenv("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
    start = client.get("/auth/google", follow_redirects=False)
    assert start.status_code == 307
    assert "code_challenge=" in start.headers["location"]
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    callback = client.get(f"/auth/callback?state={state}&code=auth-code", follow_redirects=False)
    assert callback.status_code == 307
    assert callback.headers["location"] == "/"
    assert "session=" in callback.headers["set-cookie"]
    assert client.get("/api/library").status_code == 200


def test_pwa_shell_manifest_and_service_worker(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    client = TestClient(create_app(cfg, seeded_db(cfg.db_path)))
    page = client.get("/").text
    assert "viewport-fit=cover" in page
    assert '<div id="root"></div>' in page
    assert "/static/assets/index-" in page
    assert 'id="layout"' not in page
    asset = page.split('href="/static/', 1)[1].split('"', 1)[0]
    assert client.get(f"/static/{asset}").status_code == 200
    assert client.get("/book/book-key").status_code == 200
    assert client.get("/manifest.webmanifest").json()["display"] == "standalone"
    worker = client.get("/sw.js")
    assert worker.status_code == 200
    assert "url.pathname !== '/'" in worker.text
    assert "Service-Worker-Allowed" in worker.headers
    assert client.get("/favicon.ico").status_code == 200


def test_missing_session_secret_rejects_known_fallback_cookie_and_startup(tmp_path: Path) -> None:
    import base64

    from itsdangerous import TimestampSigner

    cfg = config(tmp_path)
    cfg.secret = ""
    app = create_app(cfg, seeded_db(cfg.db_path))
    client = TestClient(app)
    payload = base64.b64encode(json.dumps({"email": cfg.allowed_email}).encode())
    cookie = TimestampSigner("development-only-change-me-set-APP_SECRET_KEY").sign(payload)
    client.cookies.set("session", cookie.decode())
    assert client.get("/api/features").status_code == 401
    with pytest.raises(RuntimeError, match="APP_SECRET_KEY"), TestClient(app):
        pass


def test_book_detail_includes_saved_progress(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    db = seeded_db(cfg.db_path)
    db.save_progress("book-key", "drive-track", 12.5)
    client = TestClient(create_app(cfg, db))
    sign_in(client)
    progress = client.get("/api/books/book-key").json()["progress"]
    assert progress["track_id"] == "drive-track"
    assert progress["position"] == 12.5
    assert progress["updated_at"].endswith("Z")


def test_metadata_search_shows_google_books_and_open_library_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    import audioscan.web as web

    responses = [
        {
            "items": [
                {"id": "google-id", "volumeInfo": {"title": "Book", "authors": ["Author"]}}
            ]
        },
        {
            "docs": [
                {"key": "/works/OL123W", "title": "Book", "author_name": ["Author"], "cover_i": 123}
            ]
        },
    ]

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *args: Any) -> None:
            pass

        async def get(self, url: str, **kwargs: Any) -> httpx.Response:
            return httpx.Response(200, json=responses.pop(0), request=httpx.Request("GET", url))

    monkeypatch.setattr(web.httpx, "AsyncClient", FakeClient)
    cfg = config(tmp_path)
    client = TestClient(create_app(cfg, seeded_db(cfg.db_path)))
    sign_in(client)
    candidates = client.post("/api/books/book-key/metadata/search").json()["candidates"]
    assert [item["source"] for item in candidates] == ["Google Books", "Open Library"]
    assert candidates[1]["cover"].endswith("123-M.jpg")


def test_metadata_search_keeps_open_library_results_when_google_books_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    import audioscan.web as web

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            self.calls = 0

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *args: Any) -> None:
            pass

        async def get(self, url: str, **kwargs: Any) -> httpx.Response:
            if "googleapis.com" in url:
                raise httpx.HTTPStatusError(
                    "429 Too Many Requests",
                    request=httpx.Request("GET", url),
                    response=httpx.Response(429, request=httpx.Request("GET", url)),
                )
            return httpx.Response(
                200,
                json={
                    "docs": [
                        {
                            "key": "/works/OL27258W",
                            "title": "Neuromancer",
                            "author_name": ["William Gibson"],
                        }
                    ]
                },
                request=httpx.Request("GET", url),
            )

    monkeypatch.setattr(web.httpx, "AsyncClient", FakeClient)
    cfg = config(tmp_path)
    client = TestClient(create_app(cfg, seeded_db(cfg.db_path)))
    sign_in(client)
    result = client.post("/api/books/book-key/metadata/search")
    assert result.status_code == 200
    assert result.json()["candidates"][0]["source"] == "Open Library"
    assert result.json()["source_errors"] == ["Google Books"]


def test_manual_book_metadata_overrides_survive_library_rescan(tmp_path: Path) -> None:
    db = seeded_db(tmp_path / "library.sqlite3")
    updated = db.update_book_metadata(
        "book-key",
        {
            "title": "Improved Book Title",
            "artist": "Better Author",
            "cover": "https://cover.test/book.jpg",
            "description": "Description",
            "unknown": "ignored",
        },
    )
    assert updated is not None
    assert updated["title"] == "Improved Book Title"
    db.save_library(
        [{"id": "book-key", "title": "Book", "artist": "Author", "duration": 60,
          "tracks": [{"id": "drive-track"}]}],
        [{"id": "drive-track", "book_id": "book-key", "name": "Book.m4b",
          "path": "Book.m4b", "size": 16, "mime_type": "audio/mp4"}],
    )
    rescanned = db.book("book-key")
    assert rescanned is not None
    assert rescanned["title"] == "Improved Book Title"
    assert rescanned["artist"] == "Better Author"
    assert rescanned["cover"] == "https://cover.test/book.jpg"
    assert rescanned["description"] == "Description"


def test_scan_subscription_does_not_replay_an_old_completion(tmp_path: Path) -> None:
    import asyncio

    cfg = config(tmp_path)
    app = create_app(cfg, seeded_db(cfg.db_path))
    scanner = app.state.scanner
    scanner.publish(status="completed", total=1, processed=1)
    scanner.publish(status="running", total=2, processed=0)
    endpoint = next(route.endpoint for route in app.routes if getattr(route, "path", "") == "/api/library/events")

    class FakeRequest:
        session = {"email": cfg.allowed_email, "generation": app.state.db.auth_generation()}

        async def is_disconnected(self) -> bool:
            return False

    async def read_events() -> None:
        response = endpoint(FakeRequest())
        iterator = response.body_iterator
        first = await anext(iterator)
        second = await anext(iterator)
        assert '"status": "running"' in first
        assert '"status": "running"' in second
        await iterator.aclose()

    asyncio.run(read_events())
