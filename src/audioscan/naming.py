"""Recover book/chapter structure from file names.

Dumps from real libraries are messy: ``Chapter 31; The Battle of Hogwarts.mp3``,
``01 - Track.mp3``, and interrupted ``....mp3.part`` downloads all sit in the same
folder. This module extracts a chapter number and title where it can, then groups
files back into books and reports gaps and duplicates.
"""

from __future__ import annotations

import json
import posixpath
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from .models import TrackMeta

_AUDIO_EXT_RE = re.compile(
    r"\.(mp3|mp2|m4a|m4b|m4p|mp4|aac|flac|ogg|oga|opus|spx|wav|wave|aif|aiff|wma|mka|ape|mpc|tta|dsf|dff)$",
    re.IGNORECASE,
)

#: Suffixes that mark an incomplete download. Longest first so that ".partial"
#: is never mistaken for ".part".
PARTIAL_SUFFIXES = (".crdownload", ".download", ".partial", ".part", ".tmp", ".!ut")
_REVISION_RE = re.compile(r"\s*\(\s*(\d+)\s*\)?\s*$")

# Ordered most-specific first; each entry is (regex, confidence, pattern name).
_PATTERNS: list[tuple[re.Pattern[str], str, str]] = [
    (
        re.compile(
            r"^(?:chapter|chap|ch)\s*[._-]?\s*(?P<num>\d{1,4})\s*"
            r"(?:[;:,]|\s[-–—]\s*|\s+)\s*(?P<title>.+)$",
            re.IGNORECASE,
        ),
        "high",
        "chapter-number-semicolon-title",
    ),
    (
        re.compile(
            r"^(?P<book>.+?)\s[-–—]\s*(?:chapter\s*)?(?P<num>\d{1,4})\s*[-–—:]\s*(?P<title>.+)$",
            re.IGNORECASE,
        ),
        "high",
        "book-dash-number-dash-title",
    ),
    (
        re.compile(r"^(?P<book>.+?)\s*;\s*(?P<num>\d{1,4})\s*[._-]?\s*(?P<title>.+)$"),
        "medium",
        "book-semicolon-number-title",
    ),
    (
        re.compile(r"^(?P<num>\d{1,4})\s*[-–—._)\]]\s*(?P<title>.+)$"),
        "medium",
        "number-separator-title",
    ),
    (
        re.compile(
            r"^(?:track|part|pt|disc)\s*[._-]?\s*(?P<num>\d{1,4})\s*[-–—:._]?\s*(?P<title>.*)$",
            re.IGNORECASE,
        ),
        "medium",
        "track-number-title",
    ),
]


@dataclass(slots=True)
class ParsedName:
    """What a file name told us about a track."""

    raw: str = ""
    stem: str = ""
    extension: str = ""
    is_partial: bool = False
    revision: int | None = None
    number: int | None = None
    title: str | None = None
    book: str | None = None
    confidence: str = "none"
    pattern: str = "unmatched"

    @property
    def is_unnumbered(self) -> bool:
        """True when no chapter/track number could be recovered."""
        return self.number is None

    def to_dict(self) -> dict[str, object]:
        return {
            "raw": self.raw,
            "stem": self.stem,
            "extension": self.extension,
            "is_partial": self.is_partial,
            "revision": self.revision,
            "number": self.number,
            "title": self.title,
            "book": self.book,
            "confidence": self.confidence,
            "pattern": self.pattern,
        }


@dataclass(slots=True)
class GroupedTrack:
    """A track together with the position parsed from its file name."""

    track: TrackMeta
    parsed: ParsedName
    number: int | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "number": self.number,
            "name": self.track.name,
            "path": self.track.path,
            "parsed": self.parsed.to_dict(),
            "title": self.track.display_title,
            "duration": self.track.duration,
            "error": self.track.error,
        }


@dataclass(slots=True)
class BookGroup:
    """A set of tracks believed to belong to one book."""

    key: str
    title: str
    tracks: list[GroupedTrack] = field(default_factory=list)
    missing: list[int] = field(default_factory=list)
    duplicates: list[int] = field(default_factory=list)
    unnumbered: int = 0

    @property
    def count(self) -> int:
        """Number of tracks in this group."""
        return len(self.tracks)

    @property
    def total_duration(self) -> float:
        """Summed duration of every readable track, in seconds."""
        return sum(t.track.duration or 0.0 for t in self.tracks)

    @property
    def problems(self) -> list[str]:
        """Short human-readable issues found in this group."""
        issues: list[str] = []
        if self.missing:
            issues.append(f"missing {_range_text(self.missing)}")
        if self.duplicates:
            issues.append(f"duplicate {_range_text(self.duplicates)}")
        if self.unnumbered:
            issues.append(f"{self.unnumbered} unnumbered")
        partials = [t for t in self.tracks if t.parsed.is_partial]
        if partials:
            issues.append(f"{len(partials)} partial download")
        errored = [t for t in self.tracks if not t.track.is_ok]
        if errored:
            issues.append(f"{len(errored)} unreadable")
        return issues

    def to_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "title": self.title,
            "count": self.count,
            "total_duration": round(self.total_duration, 3),
            "missing": self.missing,
            "duplicates": self.duplicates,
            "unnumbered": self.unnumbered,
            "problems": self.problems,
            "tracks": [t.to_dict() for t in self.tracks],
        }


def _range_text(numbers: Sequence[int]) -> str:
    """Condense ``[1, 2, 3, 7]`` into ``1-3, 7``."""
    if not numbers:
        return ""
    ordered = sorted(set(numbers))
    chunks: list[str] = []
    start = previous = ordered[0]
    for number in ordered[1:]:
        if number == previous + 1:
            previous = number
            continue
        chunks.append(f"{start}" if start == previous else f"{start}-{previous}")
        start = previous = number
    chunks.append(f"{start}" if start == previous else f"{start}-{previous}")
    return ", ".join(chunks)


def strip_partial_suffixes(name: str) -> str:
    """Remove every trailing partial-download suffix (``book.mp3.part``)."""
    lowered = (name or "").strip().lower()
    while True:
        for suffix in PARTIAL_SUFFIXES:
            if lowered.endswith(suffix):
                lowered = lowered[: -len(suffix)]
                break
        else:
            return lowered


def split_extension(name: str) -> tuple[str, str, bool]:
    """Split a file name into ``(stem, extension, is_partial_download)``.

    Handles interrupted downloads such as ``Book (2.mp3.part``.
    """
    work = (name or "").strip()
    stripped = strip_partial_suffixes(work)
    partial = stripped != work.lower()
    work = work[: len(stripped)] if partial else work
    match = _AUDIO_EXT_RE.search(work)
    if match:
        return work[: match.start()].strip(), match.group(0).lstrip(".").lower(), partial
    return work.strip(), "", partial


def parse_name(name: str) -> ParsedName:
    """Best-effort extraction of a chapter number and title from a file name."""
    parsed = ParsedName(raw=name or "")
    stem, extension, partial = split_extension(name)
    parsed.stem = stem
    parsed.extension = extension
    parsed.is_partial = partial
    if not stem:
        return parsed

    revision_match = _REVISION_RE.search(stem)
    if revision_match:
        parsed.revision = int(revision_match.group(1))
        stem = stem[: revision_match.start()].strip() or stem

    for regex, confidence, pattern_name in _PATTERNS:
        match = regex.match(stem)
        if not match:
            continue
        groups = match.groupdict()
        title = (groups.get("title") or "").strip(" -–—._;:") or None
        book = (groups.get("book") or "").strip(" -–—._;:") or None
        number_text = groups.get("num")
        parsed.number = int(number_text) if number_text else None
        parsed.title = title or stem
        parsed.book = book
        parsed.confidence = confidence
        parsed.pattern = pattern_name
        return parsed

    parsed.title = stem
    parsed.confidence = "low"
    return parsed


def normalize_key(text: str) -> str:
    """Fold a string into a comparison key (lowercase alphanumerics)."""
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def find_gaps(numbers: Iterable[int], *, max_span: int = 500) -> list[int]:
    """Chapter numbers missing between the lowest and highest present."""
    unique = sorted({int(n) for n in numbers if n and int(n) > 0})
    if len(unique) < 2:
        return []
    span = unique[-1] - unique[0] + 1
    if span < 2 or span > max_span:
        return []
    present = set(unique)
    return [n for n in range(unique[0], unique[-1] + 1) if n not in present]


def find_duplicates(numbers: Iterable[int]) -> list[int]:
    """Chapter numbers that appear more than once."""
    counts = Counter(int(n) for n in numbers if n)
    return sorted(number for number, count in counts.items() if count > 1)


def _group_identity(track: TrackMeta, parsed: ParsedName) -> tuple[str, str]:
    """Decide which book a track belongs to, preferring embedded album tags."""
    album = (track.album or "").strip()
    if album:
        album_artist = (track.albumartist or track.artist or "").strip()
        parent = posixpath.dirname((track.path or "").replace("\\", "/"))
        # Disc subfolders belong to their edition, not to separate books.
        if re.fullmatch(r"(?:disc|disk|cd)[ _.-]*\d+", posixpath.basename(parent), re.I):
            parent = posixpath.dirname(parent)
        return json.dumps(["album", parent, album_artist, album], ensure_ascii=False), album

    path = (track.path or "").replace("\\", "/")
    parent = posixpath.dirname(path)
    if parent:
        return json.dumps(["folder", parent], ensure_ascii=False), posixpath.basename(
            parent
        ) or parent

    if parsed.book:
        return json.dumps(["name", parsed.book], ensure_ascii=False), parsed.book

    return json.dumps(["track", track.id or track.name], ensure_ascii=False), track.display_title


def _sort_key(item: GroupedTrack) -> tuple[int, int, str]:
    if item.number is None:
        return (1, 0, (item.track.name or "").lower())
    return (0, item.number, "")


def group_tracks(tracks: Sequence[TrackMeta]) -> list[BookGroup]:
    """Group tracks into books and annotate gaps, duplicates and partials."""
    buckets: dict[str, list[GroupedTrack]] = {}
    titles: dict[str, str] = {}

    for track in tracks:
        parsed = parse_name(track.name or track.path)
        key, title = _group_identity(track, parsed)
        buckets.setdefault(key, []).append(
            GroupedTrack(track=track, parsed=parsed, number=parsed.number)
        )
        titles.setdefault(key, title)

    groups: list[BookGroup] = []
    for key, items in buckets.items():
        items.sort(key=_sort_key)
        numbers = [item.number for item in items if item.number is not None]
        groups.append(
            BookGroup(
                key=key,
                title=titles.get(key, key),
                tracks=items,
                missing=find_gaps(numbers),
                duplicates=find_duplicates(numbers),
                unnumbered=sum(1 for item in items if item.number is None),
            )
        )
    groups.sort(key=lambda group: (-group.count, group.title.lower()))
    return groups


__all__ = [
    "PARTIAL_SUFFIXES",
    "BookGroup",
    "GroupedTrack",
    "ParsedName",
    "find_duplicates",
    "find_gaps",
    "group_tracks",
    "normalize_key",
    "parse_name",
    "split_extension",
    "strip_partial_suffixes",
]
