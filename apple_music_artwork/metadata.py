"""Audio discovery, embedded metadata reading, and album grouping."""

from __future__ import annotations

import math
import os
import re
import stat
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Mapping
from pathlib import Path

import mutagen

from .constants import AUDIO_EXTENSIONS, MAX_TAG_TEXT
from .filesystem import _open_regular_source, _stat_identity
from .matching import _normalize_barcode, _normalize_release_id, normalize_text
from .models import AlbumGroup, EmbedError, TrackMetadata
from .mutagen_io import _load_mutagen


def _clean_untrusted_text(value: object, *, maximum: int = MAX_TAG_TEXT) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    text = str(value)
    if len(text) > maximum:
        return ""
    cleaned = "".join(
        " " if unicodedata.category(character).startswith("C") else character for character in text
    )
    return " ".join(cleaned.split())


def _terminal_safe(value: object) -> str:
    text = str(value)
    return "".join(
        " " if unicodedata.category(character).startswith("C") else character for character in text
    )


def _first_tag(tags: Mapping[str, object] | None, *names: str) -> str:
    if not tags:
        return ""
    lowered = {str(key).casefold(): value for key, value in tags.items()}
    for name in names:
        value = lowered.get(name.casefold())
        if isinstance(value, (list, tuple)):
            value = value[0] if value else ""
        if value is not None:
            cleaned = _clean_untrusted_text(value)
            if cleaned:
                return cleaned
    return ""


def _number_pair(value: str) -> tuple[int | None, int | None]:
    if not value or len(value) > 32:
        return None, None
    match = re.fullmatch(r"\s*(\d{1,4})(?:\s*/\s*(\d{1,4}))?\s*", value)
    if not match:
        return None, None
    number = int(match.group(1))
    total = int(match.group(2)) if match.group(2) else None
    if number < 1 or (total is not None and (total < number or total > 9999)):
        return None, None
    return number, total


def _year(value: str) -> int | None:
    match = re.search(r"(?<!\d)(?:18|19|20|21)\d{2}(?!\d)", value)
    return int(match.group()) if match else None


def discover_audio_files(root: Path) -> list[Path]:
    """Recursively discover only regular, non-symlinked files contained by root."""
    root = root.expanduser()
    try:
        root_info = root.lstat()
    except OSError:
        return []
    if root.is_symlink() or not stat.S_ISDIR(root_info.st_mode):
        return []
    root_resolved = root.resolve()
    discovered: list[Path] = []
    for path in root.rglob("*"):
        try:
            info = path.lstat()
            relative = path.relative_to(root)
            resolved = path.resolve(strict=True)
        except (OSError, ValueError):
            continue
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_nlink != 1:
            continue
        if not resolved.is_relative_to(root_resolved):
            continue
        if path.suffix.casefold() not in AUDIO_EXTENSIONS:
            continue
        if any(part.startswith(".") for part in relative.parts):
            continue
        discovered.append(path)
    return sorted(discovered, key=str)


def read_track_metadata(
    path: Path,
    *,
    expected_identity: tuple[int, int, int, int, int] | None = None,
) -> TrackMetadata | None:
    """Read tags and headers while binding the result to one safely opened object."""
    parent_descriptor = -1
    source_descriptor = -1
    try:
        parent_descriptor, source_descriptor, before = _open_regular_source(
            path,
            expected_identity,
        )
        source_identity = _stat_identity(before)
        audio = _load_mutagen(source_descriptor, easy=True, filename=path)
        after = os.fstat(source_descriptor)
    except (EmbedError, mutagen.MutagenError, OSError):
        return None
    finally:
        if source_descriptor >= 0:
            os.close(source_descriptor)
        if parent_descriptor >= 0:
            os.close(parent_descriptor)
    if audio is None or _stat_identity(after) != source_identity:
        return None

    tags = audio.tags
    title = _first_tag(tags, "title")
    album = _first_tag(tags, "album")
    artist = _first_tag(tags, "artist")
    album_artist = _first_tag(tags, "albumartist", "album artist") or artist
    track_number, track_total = _number_pair(_first_tag(tags, "tracknumber"))
    disc_number, disc_total = _number_pair(_first_tag(tags, "discnumber"))
    date = _first_tag(tags, "date", "year", "originaldate")
    barcode = _first_tag(tags, "barcode", "upc") or None
    release_id = _first_tag(tags, "musicbrainz_albumid", "musicbrainz release id") or None
    duration = getattr(getattr(audio, "info", None), "length", None)
    duration_ms: int | None = None
    try:
        numeric_duration = float(duration) if duration is not None else None
        if (
            numeric_duration is not None
            and math.isfinite(numeric_duration)
            and 0 < numeric_duration <= 86_400
        ):
            duration_ms = round(numeric_duration * 1_000)
    except (TypeError, ValueError, OverflowError):
        duration_ms = None

    return TrackMetadata(
        path=path,
        title=title,
        artist=artist,
        album=album,
        album_artist=album_artist,
        year=_year(date),
        track_number=track_number,
        track_total=track_total,
        disc_number=disc_number,
        disc_total=disc_total,
        duration_ms=duration_ms,
        barcode=_normalize_barcode(barcode),
        musicbrainz_release_id=_normalize_release_id(release_id),
        source_identity=source_identity,
    )


def _release_key(track: TrackMetadata) -> tuple[object, ...]:
    identity = (
        normalize_text(track.album_artist or track.artist),
        normalize_text(track.album),
        track.year,
    )
    release_id = _normalize_release_id(track.musicbrainz_release_id)
    barcode = _normalize_barcode(track.barcode)
    if release_id:
        return ("musicbrainz", release_id, *identity)
    if barcode:
        return ("barcode", barcode, *identity)
    return ("tags", *identity)


def _logical_track_key(track: TrackMetadata) -> tuple[object, ...]:
    disc = track.disc_number or 1
    if track.track_number:
        return (disc, track.track_number, normalize_text(track.title))
    duration_bucket = round((track.duration_ms or 0) / 2_000)
    return (disc, normalize_text(track.title), duration_bucket)


def group_tracks(tracks: Iterable[TrackMetadata]) -> list[AlbumGroup]:
    """Group by release tags and collapse duplicate encodings of each logical track."""
    buckets: dict[tuple[object, ...], list[TrackMetadata]] = defaultdict(list)
    for track in tracks:
        if track.album and track.title and (track.album_artist or track.artist):
            buckets[_release_key(track)].append(track)

    groups: list[AlbumGroup] = []
    for members in buckets.values():
        logical: dict[tuple[object, ...], TrackMetadata] = {}
        for track in sorted(members, key=lambda item: str(item.path)):
            logical.setdefault(_logical_track_key(track), track)
        first = members[0]
        groups.append(
            AlbumGroup(
                album=first.album,
                album_artist=first.album_artist or first.artist,
                year=first.year,
                files=tuple(sorted((track.path for track in members), key=str)),
                logical_tracks=tuple(logical.values()),
                barcode=_normalize_barcode(first.barcode),
                musicbrainz_release_id=_normalize_release_id(first.musicbrainz_release_id),
                source_identities=tuple(
                    (track.path, track.source_identity)
                    for track in sorted(members, key=lambda item: str(item.path))
                    if track.source_identity is not None
                ),
            )
        )
    return sorted(
        groups, key=lambda group: (normalize_text(group.album_artist), normalize_text(group.album))
    )


__all__ = (
    "discover_audio_files",
    "group_tracks",
    "read_track_metadata",
)
