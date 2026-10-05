"""Turn a seekable byte stream into a :class:`TrackMeta` using mutagen.

Tag names differ per container (``TIT2`` vs ``©nam`` vs ``title``). Rather than
open the file several times with different ``mutagen`` options, this module maps
the handful of fields we care about per container family and keeps everything
else in a raw ``tags`` dict.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import mutagen

from .chapters import decode_flac_picture, extract_chapters
from .models import SOURCE_LOCAL, Cover, TrackMeta

_FORMAT_NAMES = {
    "MP3": "MP3",
    "MP4": "MP4",
    "FLAC": "FLAC",
    "OggVorbis": "Ogg Vorbis",
    "OggOpus": "Ogg Opus",
    "OggFlac": "Ogg FLAC",
    "OggSpeex": "Ogg Speex",
    "OggTheora": "Ogg Theora",
    "WAVE": "WAV",
    "AIFF": "AIFF",
    "WavPack": "WavPack",
    "MonkeysAudio": "Monkey's Audio",
    "Musepack": "Musepack",
    "TrueAudio": "TTA",
    "OptimFROG": "OptimFROG",
    "ASF": "WMA",
    "AAC": "AAC (ADTS)",
    "AC3": "AC-3",
    "DSF": "DSF",
    "DFF": "DSDIFF",
}

_ID3_TEXT = {
    "title": "TIT2",
    "artist": "TPE1",
    "album": "TALB",
    "albumartist": "TPE2",
    "track": "TRCK",
    "disc": "TPOS",
    "genre": "TCON",
    "date": "TDRC",
}
_ID3_DATE_FALLBACKS = ("TYER", "TDOR", "TORY")

_MP4_TEXT = {
    "title": "\xa9nam",
    "artist": "\xa9ART",
    "album": "\xa9alb",
    "albumartist": "aART",
    "genre": "\xa9gen",
    "date": "\xa9day",
    "comment": "\xa9cmt",
}

_VORBIS_TEXT = {
    "title": ("title",),
    "artist": ("artist",),
    "album": ("album",),
    "albumartist": ("albumartist", "album artist", "album-artist"),
    "track": ("tracknumber", "track"),
    "disc": ("discnumber", "disc"),
    "genre": ("genre",),
    "date": ("date", "year"),
    "comment": ("comment", "description"),
}

_BINARY_KEYS = {
    "APIC",
    "PIC",
    "covr",
    "pictures",
    "metadata_block_picture",
    "GEOB",
    "PRIV",
    "MCDI",
    "UFID",
    "PCNT",
    "POPM",
    "RVA2",
    "CHAP",
    "CTOC",
}

_COVER_MIME_BY_MP4_TYPE = {
    13: "image/jpeg",
    14: "image/png",
    27: "image/bmp",
}


def _first(value: Any) -> Any:
    """Unwrap mutagen's list-valued tags to a single scalar."""
    if isinstance(value, (list, tuple)) and value and not isinstance(value[0], (int, float)):
        return _first(value[0])
    return value


def _as_text(value: Any) -> str | None:
    """Coerce a tag value (str/bytes/MP4FreeForm/frame) into clean text."""
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace").strip() or None
    if isinstance(value, tuple):
        return "/".join(str(part) for part in value if part)
    if hasattr(value, "decode") and hasattr(value, "encode"):
        try:
            return value.decode("utf-8", "replace").strip() or None
        except Exception:
            return None
    return str(value).strip() or None


def _tag_family(audio: Any, tags: Any) -> str:
    """Identify which tag convention a parsed file uses."""
    kind = type(audio).__name__
    if kind == "MP4":
        return "mp4"
    if tags is not None and type(tags).__name__ == "ID3":
        return "id3"
    if tags is not None and hasattr(tags, "getall"):
        return "id3"
    if tags is not None and hasattr(tags, "keys"):
        return "vorbis"
    if hasattr(audio, "keys"):
        return "vorbis"
    return "none"


def _id3_text(tags: Any, key: str) -> str | None:
    frame = tags.get(key)
    if frame is None:
        return None
    text = getattr(frame, "text", None)
    if text:
        return _as_text(_first(text))
    return _as_text(frame)


def _id3_comment(tags: Any) -> str | None:
    try:
        frames = list(tags.getall("COMM"))
    except Exception:  # pragma: no cover - defensive
        return None
    for frame in frames:
        text = getattr(frame, "text", None)
        value = _as_text(_first(text)) if text else None
        if value:
            return value
    return None


def _apply_id3(meta: TrackMeta, tags: Any) -> None:
    for field, key in _ID3_TEXT.items():
        value = _id3_text(tags, key)
        if value:
            setattr(meta, field, value)
    if not meta.date:
        for key in _ID3_DATE_FALLBACKS:
            value = _id3_text(tags, key)
            if value:
                meta.date = value
                break
    if not meta.comment:
        meta.comment = _id3_comment(tags)


def _mp4_pair(tags: Any, key: str) -> str | None:
    value = tags.get(key)
    first = _first(value)
    if isinstance(first, tuple) and first:
        number = int(first[0]) if first[0] else 0
        total = int(first[1]) if len(first) > 1 and first[1] else 0
        if number and total:
            return f"{number}/{total}"
        if number:
            return str(number)
    return None


def _apply_mp4(meta: TrackMeta, tags: Any) -> None:
    for field, key in _MP4_TEXT.items():
        value = tags.get(key)
        text = _as_text(_first(value)) if value is not None else None
        if text:
            setattr(meta, field, text)
    meta.track = _mp4_pair(tags, "trkn")
    meta.disc = _mp4_pair(tags, "disk")


def _apply_vorbis(meta: TrackMeta, tags: Any) -> None:
    for field, keys in _VORBIS_TEXT.items():
        for key in keys:
            try:
                value = tags.get(key)
            except Exception:  # pragma: no cover - defensive
                continue
            text = _as_text(_first(value)) if value is not None else None
            if text:
                setattr(meta, field, text)
                break


def _collect_extra_tags(audio: Any, family: str) -> dict[str, str]:
    """Keep every remaining textual tag so nothing is silently dropped."""
    tags = getattr(audio, "tags", None)
    if tags is None:
        tags = audio if hasattr(audio, "keys") else None
    if tags is None:
        return {}

    try:
        keys = sorted(str(key) for key in tags)
    except Exception:  # pragma: no cover - defensive
        return {}

    extras: dict[str, str] = {}
    for key in keys:
        if key in _BINARY_KEYS:
            continue
        if family == "id3" and not (key.startswith("T") or key in {"COMM", "USLT", "WXXX", "GRP1"}):
            continue
        try:
            value = tags.get(key)
        except Exception:  # pragma: no cover - defensive
            continue
        text = _as_text(_first(value))
        if text:
            extras[key] = text
    return extras


def _cover_objects(audio: Any) -> list[tuple[str | None, str | None, bytes]]:
    """Return ``(mime, description, data)`` for every embedded picture."""
    results: list[tuple[str | None, str | None, bytes]] = []
    tags = getattr(audio, "tags", None)
    if tags is None:
        tags = audio if hasattr(audio, "keys") else None

    if tags is not None and hasattr(tags, "getall"):
        try:
            for apic in tags.getall("APIC"):
                data = bytes(getattr(apic, "data", b"") or b"")
                mime = getattr(apic, "mime", None)
                description = getattr(apic, "desc", None) or None
                results.append((mime, description, data))
        except Exception:  # pragma: no cover - defensive
            pass
        if results:
            return results

    if tags is not None and hasattr(tags, "get"):
        try:
            covr = tags.get("covr")
        except Exception:  # pragma: no cover - defensive
            covr = None
        if covr:
            for entry in covr:
                image_format = getattr(entry, "imageformat", None)
                mime = None
                if image_format is not None:
                    mime = _COVER_MIME_BY_MP4_TYPE.get(int(image_format))
                results.append((mime, None, bytes(entry)))
            if results:
                return results

    pictures = getattr(audio, "pictures", None)
    if pictures:
        for picture in pictures:
            data = bytes(getattr(picture, "data", b"") or b"")
            mime = getattr(picture, "mime", None)
            description = getattr(picture, "desc", None) or None
            results.append((mime, description, data))
        if results:
            return results

    if tags is not None and hasattr(tags, "get"):
        try:
            blocks = tags.get("metadata_block_picture")
        except Exception:  # pragma: no cover - defensive
            blocks = None
        for encoded in blocks or []:
            try:
                picture = decode_flac_picture(encoded)
            except Exception:  # pragma: no cover - malformed base64
                continue
            data = bytes(getattr(picture, "data", b"") or b"")
            mime = getattr(picture, "mime", None)
            description = getattr(picture, "desc", None) or None
            results.append((mime, description, data))

    return results


def _format_name(audio: Any) -> str:
    kind = type(audio).__name__
    return _FORMAT_NAMES.get(kind, kind)


def _audio_mime(audio: Any) -> str | None:
    mimes = getattr(audio, "mime", None)
    if not mimes:
        return None
    if isinstance(mimes, (list, tuple)):
        return str(mimes[0]) if mimes else None
    return str(mimes)


def _codec_name(audio: Any) -> str | None:
    info = getattr(audio, "info", None)
    if info is None:
        return None
    codec = getattr(info, "codec", None)
    description = getattr(info, "codec_description", None)
    if codec and description:
        return f"{codec} ({description})"
    if codec:
        return str(codec)
    if description:
        return str(description)

    kind = type(audio).__name__
    if kind == "MP3":
        version = getattr(info, "version", None)
        layer = getattr(info, "layer", None)
        if version and layer:
            rendered = f"{version:g}" if isinstance(version, float) else str(version)
            return f"MPEG {rendered} Layer {int(layer)}"
    if kind == "FLAC":
        bits = getattr(info, "bits_per_sample", None)
        return f"FLAC {bits}-bit" if bits else "FLAC"
    return None


def _apply_info(meta: TrackMeta, audio: Any) -> None:
    info = getattr(audio, "info", None)
    if info is not None:
        length = getattr(info, "length", None)
        meta.duration = float(length) if length else None
        bitrate = getattr(info, "bitrate", None)
        meta.bitrate = int(bitrate) if bitrate else None
        sample_rate = getattr(info, "sample_rate", None)
        meta.sample_rate = int(sample_rate) if sample_rate else None
        channels = getattr(info, "channels", None)
        meta.channels = int(channels) if channels else None
    meta.codec = _codec_name(audio)


def _finalise(meta: TrackMeta) -> None:
    """Fill in values mutagen could not report, e.g. bitrate without an esds box."""
    if not meta.bitrate and meta.duration and meta.size:
        meta.bitrate = int(meta.size * 8 / meta.duration)


def _rewind(stream: Any) -> None:
    """Seek back to the start before parsing.

    ``mutagen.File()`` sniffs the container from the *current* position and never
    rewinds, so re-probing an already-read stream (for example to pull a cover
    out of it afterwards) would otherwise fail with "unrecognised audio format".
    """
    seek = getattr(stream, "seek", None)
    if seek is None:
        return
    try:
        seek(0)
    except Exception:  # pragma: no cover - non-seekable streams
        return


def probe(
    fileobj: Any,
    *,
    name: str = "",
    path: str = "",
    source: str = SOURCE_LOCAL,
    file_id: str = "",
    size: int | None = None,
    mime_type: str | None = None,
    modified: str | None = None,
) -> TrackMeta:
    """Read metadata from ``fileobj`` without reading the whole stream."""
    meta = TrackMeta(
        source=source,
        id=file_id or path or name,
        name=name or (Path(path).name if path else ""),
        path=path or name,
        size=size,
        mime_type=mime_type,
        modified=modified,
    )
    _rewind(fileobj)
    try:
        audio = mutagen.File(fileobj)
    except Exception as exc:
        from .reader import FetchError

        meta.error = f"{type(exc).__name__}: {exc}"
        meta.retryable_error = isinstance(exc, FetchError)
        return meta

    if audio is None:
        suffix = Path(name or path).suffix.lower()
        if suffix in {".m4a", ".m4b", ".m4p", ".mp4"}:
            fileobj.seek(0)
            header = fileobj.read(128)
            meta.error = (
                f"unrecognised audio format: {suffix} file has no MP4 'ftyp' signature "
                f"in its first 128 bytes (header: {header[:16].hex()})"
            )
        else:
            meta.error = "unrecognised audio format"
        return meta

    meta.format = _format_name(audio)
    if not meta.mime_type:
        meta.mime_type = _audio_mime(audio)
    _apply_info(meta, audio)

    family = _tag_family(audio, getattr(audio, "tags", None))
    tags = getattr(audio, "tags", None)
    try:
        if family == "id3" and tags is not None:
            _apply_id3(meta, tags)
        elif family == "mp4" and tags is not None:
            _apply_mp4(meta, tags)
        elif family == "vorbis":
            _apply_vorbis(meta, tags if tags is not None else audio)
    except Exception as exc:  # pragma: no cover - defensive
        meta.error = f"tag parse failed: {exc}"

    try:
        meta.chapters = extract_chapters(audio)
    except Exception:  # pragma: no cover - defensive
        meta.chapters = []

    try:
        meta.covers = [
            Cover(index=index, mime=mime, description=description, size=len(data))
            for index, (mime, description, data) in enumerate(_cover_objects(audio))
        ]
    except Exception:  # pragma: no cover - defensive
        meta.covers = []

    try:
        meta.tags = _collect_extra_tags(audio, family)
    except Exception:  # pragma: no cover - defensive
        meta.tags = {}

    _finalise(meta)
    return meta


def cover_bytes(fileobj: Any, index: int = 0) -> tuple[bytes, str | None]:
    """Re-open a stream and pull the raw bytes of one embedded cover image."""
    _rewind(fileobj)
    audio = mutagen.File(fileobj)
    if audio is None:
        raise ValueError("unrecognised audio format")
    covers = _cover_objects(audio)
    if not covers:
        raise IndexError("no embedded cover art")
    try:
        mime, _description, data = covers[index]
    except IndexError as exc:
        raise IndexError(f"cover index {index} out of range (found {len(covers)})") from exc
    return data, mime


__all__ = ["cover_bytes", "probe"]
