"""Serialisable data models shared by every layer of audioscan."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .fmt import format_bitrate, format_bytes, format_duration

SOURCE_LOCAL = "local"
SOURCE_GDRIVE = "gdrive"
SOURCE_HTTP = "http"


@dataclass(slots=True)
class Cover:
    """An embedded cover image (metadata only; bytes are fetched on demand)."""

    index: int = 0
    mime: str | None = None
    description: str | None = None
    size: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Chapter:
    """A single chapter marker inside one audio file.

    ``end`` is often absent in the wild; when a file exposes only start offsets
    (the common MP3/M4B layout) the next chapter's start is used as the end.
    """

    number: int = 1
    start: float = 0.0
    end: float | None = None
    title: str | None = None

    @property
    def duration(self) -> float | None:
        """Length in seconds, or ``None`` when the end offset is unknown."""
        if self.end is None:
            return None
        return max(0.0, self.end - self.start)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["duration"] = self.duration
        data["start_hms"] = format_duration(self.start)
        return data


@dataclass(slots=True)
class TrackMeta:
    """Everything audioscan learned about one audio file."""

    # --- identity -----------------------------------------------------------
    source: str = SOURCE_LOCAL
    id: str = ""
    name: str = ""
    path: str = ""
    size: int | None = None
    mime_type: str | None = None
    modified: str | None = None

    # --- stream properties --------------------------------------------------
    format: str | None = None
    codec: str | None = None
    duration: float | None = None
    bitrate: int | None = None
    sample_rate: int | None = None
    channels: int | None = None

    # --- tags ---------------------------------------------------------------
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    albumartist: str | None = None
    track: str | None = None
    disc: str | None = None
    genre: str | None = None
    date: str | None = None
    comment: str | None = None
    tags: dict[str, str] = field(default_factory=dict)

    covers: list[Cover] = field(default_factory=list)
    chapters: list[Chapter] = field(default_factory=list)

    # --- bookkeeping --------------------------------------------------------
    error: str | None = None
    retryable_error: bool = False
    fetched_bytes: int = 0
    fetch_requests: int = 0

    @property
    def is_ok(self) -> bool:
        """True when the file was parsed without an error."""
        return self.error is None

    @property
    def has_cover(self) -> bool:
        """True when at least one embedded image was found."""
        return bool(self.covers)

    @property
    def display_title(self) -> str:
        """Best available human label: title tag, else file name."""
        return self.title or self.name

    @property
    def fetch_ratio(self) -> float | None:
        """Fraction of the file that had to be fetched, when both are known."""
        if not self.size or not self.fetched_bytes:
            return None
        return self.fetched_bytes / self.size

    def summary_row(self) -> dict[str, Any]:
        """Flat, display/CSV-friendly view of this track."""
        return {
            "source": self.source,
            "name": self.name,
            "path": self.path,
            "title": self.title or "",
            "artist": self.artist or "",
            "album": self.album or "",
            "albumartist": self.albumartist or "",
            "track": self.track or "",
            "genre": self.genre or "",
            "date": self.date or "",
            "format": self.format or "",
            "codec": self.codec or "",
            "duration": round(self.duration, 3) if self.duration is not None else "",
            "duration_hms": format_duration(self.duration),
            "bitrate": self.bitrate or "",
            "sample_rate": self.sample_rate or "",
            "channels": self.channels or "",
            "chapters": len(self.chapters),
            "covers": len(self.covers),
            "size": self.size if self.size is not None else "",
            "fetched_bytes": self.fetched_bytes,
            "error": self.error or "",
        }

    def to_dict(self) -> dict[str, Any]:
        """Full nested representation, safe for ``json.dumps``."""
        data = asdict(self)
        data["duration_hms"] = format_duration(self.duration)
        data["bitrate_human"] = format_bitrate(self.bitrate)
        data["size_human"] = format_bytes(self.size)
        data["chapters"] = [c.to_dict() for c in self.chapters]
        data["covers"] = [c.to_dict() for c in self.covers]
        return data


__all__ = [
    "SOURCE_GDRIVE",
    "SOURCE_HTTP",
    "SOURCE_LOCAL",
    "Chapter",
    "Cover",
    "TrackMeta",
]
