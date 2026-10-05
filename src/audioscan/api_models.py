"""Public API projections, separate from scanner and provider records."""

from typing import Any, Literal

from pydantic import BaseModel, Field


class ChapterResponse(BaseModel):
    number: int | None = None
    title: str | None = None
    start: float
    end: float | None = None


class TrackResponse(BaseModel):
    id: str
    book_id: str
    name: str
    title: str = ""
    path: str = ""
    duration: float = 0
    ordinal: int = 0
    format: str | None = None
    chapter_count: int = 0
    chapters: list[ChapterResponse] = Field(default_factory=list)


class ProgressResponse(BaseModel):
    book_id: str
    track_id: str
    position: float
    revision: int
    updated_at: str
    completed: bool = False
    book_position: float = 0
    fraction: float | None = None
    status: Literal["not-started", "in-progress", "completed"] = "not-started"


class BookSummary(BaseModel):
    id: str
    title: str
    artist: str | None = None
    album_artist: str | None = None
    album: str | None = None
    directory: str | None = None
    cover: str | None = None
    duration: float = 0
    track_count: int = 0
    added_at: str
    favorite: bool = False
    rating: int | None = None
    tags: list[str] = Field(default_factory=list)
    progress: ProgressResponse | None = None


class BookResponse(BookSummary):
    tracks: list[TrackResponse] = Field(default_factory=list)
    description: str = ""
    publisher: str = ""
    published_date: str = ""
    isbn: str | None = None
    metadata_source: dict[str, str] | None = None


class BookPage(BaseModel):
    items: list[BookSummary]
    next_cursor: str | None = None
    revision: int
    total: int


class ChapterPage(BaseModel):
    track_id: str
    chapters: list[ChapterResponse]
    next_offset: int | None = None
    revision: int


class PlaylistResponse(BaseModel):
    id: str
    name: str
    book_ids: list[str]
    revision: int = 0
    created_at: str | None = None


class PlaylistBooksResponse(BaseModel):
    book_ids: list[str]
    revision: int


class PlaybackResponse(BaseModel):
    book_id: str
    tracks: list[TrackResponse]
    progress: ProgressResponse | None
    revision: int


class HistoryEntry(BaseModel):
    id: int
    book_id: str
    track_id: str
    position: float
    played_at: str


class HistoryPage(BaseModel):
    items: list[HistoryEntry]
    next_cursor: int | None = None


class StorageResponse(BaseModel):
    tracks: int
    bytes: int  # Compatibility alias for source_bytes.
    source_bytes: int
    cached_bytes: int
    cache_budget_bytes: int
    cache_enabled: bool
    formats: list[dict[str, Any]]


class ScanResponse(BaseModel):
    status: str = "idle"
    total: int = 0
    processed: int = 0
    current: str = ""
    error: str = ""
    job_id: str | None = None
    updated_at: str | None = None


class FeaturesResponse(BaseModel):
    favorites: list[str]
    ratings: dict[str, int]
    tags: dict[str, list[str]]
    playlists: list[PlaylistResponse]
    history: list[HistoryEntry]


class BootstrapResponse(BaseModel):
    books: list[BookResponse]
    features: FeaturesResponse
    storage: StorageResponse
    scan: ScanResponse
    revision: int


class BoundedBootstrapResponse(BaseModel):
    books: BookPage
    scan: ScanResponse
    capabilities: dict[str, bool]


class WarmResponse(BaseModel):
    status: Literal["queued", "available", "disabled", "busy", "too_large"]
