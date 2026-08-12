"""Audio discovery, embedded metadata reading, and album grouping."""

from __future__ import annotations

import math
import os
import re
import stat
import unicodedata
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

import mutagen
from mutagen.aiff import AIFF
from mutagen.easyid3 import EasyID3
from mutagen.easymp4 import EasyMP4Tags
from mutagen.id3 import ID3
from mutagen.wave import WAVE

from .constants import AUDIO_EXTENSIONS, MAX_TAG_TEXT
from .filesystem import _open_regular_source, _stat_identity
from .matching import (
    _barcode_equivalence_key,
    _duplicate_track_presentations_compatible,
    _duplicate_track_title_identity,
    _normalize_barcode,
    _normalize_release_id,
    normalize_text,
)
from .models import AlbumGroup, EmbedError, TrackMetadata
from .mutagen_io import _load_mutagen

for _easy_key, _freeform_name in (("barcode", "BARCODE"), ("upc", "UPC")):
    if _easy_key not in EasyMP4Tags.Get:
        EasyMP4Tags.RegisterFreeformKey(_easy_key, _freeform_name)
if "upc" not in EasyID3.Get:
    EasyID3.RegisterTXXXKey("upc", "UPC")


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


def _tag_values(tags: Mapping[str, object] | None, *names: str) -> tuple[str, ...]:
    if not tags:
        return ()
    lowered = {str(key).casefold(): value for key, value in tags.items()}
    values: list[str] = []
    for name in names:
        value = lowered.get(name.casefold())
        candidates = value if isinstance(value, (list, tuple)) else (value,)
        for candidate in candidates:
            if candidate is None:
                continue
            cleaned = _clean_untrusted_text(candidate)
            if cleaned:
                values.append(cleaned)
    return tuple(dict.fromkeys(values))


def _identifier_tag_values(
    tags: Mapping[str, object] | None,
    *names: str,
) -> tuple[tuple[str, ...], bool, bool]:
    """Return safely cleaned identifier values plus whether any raw tag was present."""
    if not tags:
        return (), False, False
    lowered = {str(key).casefold(): value for key, value in tags.items()}
    values: list[str] = []
    present = False
    rejected = False
    for name in names:
        key = name.casefold()
        if key not in lowered:
            continue
        value = lowered[key]
        candidates = value if isinstance(value, (list, tuple)) else (value,)
        for candidate in candidates:
            if candidate is None:
                continue
            present = True
            cleaned = _clean_untrusted_text(candidate)
            if cleaned:
                values.append(cleaned)
            else:
                rejected = True
    return tuple(dict.fromkeys(values)), present, rejected


def _first_tag(tags: Mapping[str, object] | None, *names: str) -> str:
    values = _tag_values(tags, *names)
    return values[0] if values else ""


def _identifier_tag(
    tags: Mapping[str, object] | None,
    *names: str,
    normalizer: Callable[[str | None], str | None],
    malformed_warning: str,
    conflict_warning: str,
    equivalence_key: Callable[[str | None], object] | None = None,
) -> tuple[str | None, tuple[str, ...]]:
    raw_values, tag_present, rejected_value = _identifier_tag_values(tags, *names)
    normalized_values = tuple(
        value for raw_value in raw_values if (value := normalizer(raw_value)) is not None
    )
    warnings: list[str] = []
    if tag_present and (
        rejected_value or not raw_values or len(normalized_values) != len(raw_values)
    ):
        warnings.append(malformed_warning)
    key = equivalence_key or (lambda value: value)
    keys = {key(value) for value in normalized_values}
    if len(keys) > 1:
        warnings.append(conflict_warning)
        return None, tuple(warnings)
    return (normalized_values[0] if normalized_values else None), tuple(warnings)


def _easy_id3_tags(tags: ID3) -> Mapping[str, object]:
    values: dict[str, object] = {}
    for key, getter in EasyID3.Get.items():
        try:
            values[key] = getter(tags, key)
        except (KeyError, UnicodeError, ValueError):
            continue
    return values


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


class _DiscoveredAudioFiles(list[Path]):
    """List-compatible discovery result carrying journals found in the same walk."""

    def __init__(self, files: list[Path], transaction_journals: tuple[Path, ...]) -> None:
        super().__init__(files)
        self.transaction_journals = transaction_journals


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
    transaction_journals: list[Path] = []
    for path in root.rglob("*"):
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        is_transaction_journal = (
            path.name.startswith(".")
            and ".artwork-transaction-" in path.name
            and path.name.endswith(".json")
        )
        if is_transaction_journal:
            transaction_journals.append(path)
            continue
        try:
            info = path.lstat()
        except OSError:
            continue
        try:
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
    return _DiscoveredAudioFiles(
        sorted(discovered, key=str),
        tuple(sorted(transaction_journals, key=str)),
    )


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
    if isinstance(audio, (WAVE, AIFF)) and isinstance(tags, ID3):
        tags = _easy_id3_tags(tags)
    title = _first_tag(tags, "title")
    album = _first_tag(tags, "album")
    artist = _first_tag(tags, "artist")
    album_artist = _first_tag(tags, "albumartist", "album artist") or artist
    track_number, track_total = _number_pair(_first_tag(tags, "tracknumber"))
    disc_number, disc_total = _number_pair(_first_tag(tags, "discnumber"))
    date = _first_tag(tags, "date", "year", "originaldate")
    barcode, barcode_warnings = _identifier_tag(
        tags,
        "barcode",
        "upc",
        normalizer=_normalize_barcode,
        malformed_warning="malformed UPC/barcode tag ignored",
        conflict_warning="conflicting UPC/barcode values within one file",
        equivalence_key=_barcode_equivalence_key,
    )
    release_id, release_warnings = _identifier_tag(
        tags,
        "musicbrainz_albumid",
        "musicbrainz release id",
        normalizer=_normalize_release_id,
        malformed_warning="malformed MusicBrainz release MBID tag ignored",
        conflict_warning="conflicting MusicBrainz release MBIDs within one file",
    )
    recording_id, recording_warnings = _identifier_tag(
        tags,
        "musicbrainz_trackid",
        "musicbrainz recording id",
        normalizer=_normalize_release_id,
        malformed_warning="malformed MusicBrainz recording MBID tag ignored",
        conflict_warning="conflicting MusicBrainz recording MBIDs within one file",
    )
    identifier_warnings = (*barcode_warnings, *release_warnings, *recording_warnings)
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
        barcode=barcode,
        musicbrainz_release_id=release_id,
        source_identity=source_identity,
        musicbrainz_recording_id=recording_id,
        identifier_warnings=identifier_warnings,
    )


def _tag_release_identity(track: TrackMetadata) -> tuple[object, ...]:
    return (
        normalize_text(track.album_artist or track.artist),
        normalize_text(track.album),
        track.year,
    )


def _logical_track_key(track: TrackMetadata) -> tuple[object, ...]:
    disc = track.disc_number or 1
    recording_id = _normalize_release_id(track.musicbrainz_recording_id)
    if recording_id:
        return ("musicbrainz-recording", recording_id, disc, track.track_number)
    if track.track_number:
        return (disc, track.track_number, normalize_text(track.title))
    duration_bucket = round((track.duration_ms or 0) / 2_000)
    return (disc, normalize_text(track.title), duration_bucket)


def _logical_position_key(track: TrackMetadata) -> tuple[object, ...] | None:
    if track.track_number is None:
        return None
    return (
        track.disc_number or 1,
        track.track_number,
        _duplicate_track_title_identity(track.title),
    )


def group_tracks(tracks: Iterable[TrackMetadata]) -> list[AlbumGroup]:
    """Group by release tags and collapse duplicate encodings of each logical track."""
    members = sorted(
        (
            track
            for track in tracks
            if track.album and track.title and (track.album_artist or track.artist)
        ),
        key=lambda track: str(track.path),
    )
    parents = list(range(len(members)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parents[right_root] = left_root

    identifier_owners: dict[tuple[str, object], int] = {}
    for index, track in enumerate(members):
        release_id = _normalize_release_id(track.musicbrainz_release_id)
        barcode = _normalize_barcode(track.barcode)
        keys: list[tuple[str, object]] = []
        if release_id is not None:
            keys.append(("musicbrainz", release_id))
        if barcode is not None:
            keys.append(("barcode", _barcode_equivalence_key(barcode)))
        keys.append(("tags", _tag_release_identity(track)))
        for key in keys:
            owner = identifier_owners.setdefault(key, index)
            union(index, owner)

    buckets: dict[int, list[TrackMetadata]] = defaultdict(list)
    for index, track in enumerate(members):
        buckets[find(index)].append(track)

    groups: list[AlbumGroup] = []
    for members in buckets.values():
        logical: dict[tuple[object, ...], TrackMetadata] = {}
        for track in sorted(members, key=lambda item: str(item.path)):
            key = _logical_track_key(track)
            position_key = _logical_position_key(track)
            compatible_key = next(
                (
                    existing_key
                    for existing_key, existing in logical.items()
                    if position_key is not None
                    and _logical_position_key(existing) == position_key
                    and _duplicate_track_presentations_compatible(existing, track)
                    and (
                        _normalize_release_id(existing.musicbrainz_recording_id) is None
                        or _normalize_release_id(track.musicbrainz_recording_id) is None
                        or _normalize_release_id(existing.musicbrainz_recording_id)
                        == _normalize_release_id(track.musicbrainz_recording_id)
                    )
                ),
                None,
            )
            if compatible_key is None:
                if key in logical and not _duplicate_track_presentations_compatible(
                    logical[key], track
                ):
                    key = (*key, "presentation", str(track.path))
                logical.setdefault(key, track)
                continue
            existing = logical[compatible_key]
            if (
                _normalize_release_id(existing.musicbrainz_recording_id) is None
                and _normalize_release_id(track.musicbrainz_recording_id) is not None
            ):
                logical[compatible_key] = track
        first = members[0]
        release_ids = {
            release_id
            for track in members
            if (release_id := _normalize_release_id(track.musicbrainz_release_id)) is not None
        }
        normalized_barcodes_by_key = {
            _barcode_equivalence_key(barcode): barcode
            for track in members
            if (barcode := _normalize_barcode(track.barcode)) is not None
        }
        conflicts: list[str] = []
        if len(release_ids) > 1:
            conflicts.append("conflicting MusicBrainz release MBIDs within the album group")
        if len(normalized_barcodes_by_key) > 1:
            conflicts.append("conflicting UPC/barcode tags within the album group")
        release_id = next(iter(release_ids)) if len(release_ids) == 1 else None
        barcode = (
            next(iter(normalized_barcodes_by_key.values()))
            if len(normalized_barcodes_by_key) == 1
            else None
        )
        musicbrainz_provenance_complete = bool(
            release_id
            and all(
                _normalize_release_id(track.musicbrainz_release_id) == release_id
                and _normalize_release_id(track.musicbrainz_recording_id) is not None
                for track in members
            )
        )
        recording_ids_by_position: dict[tuple[object, ...], set[str]] = defaultdict(set)
        for track in members:
            recording_id = _normalize_release_id(track.musicbrainz_recording_id)
            position_key = _logical_position_key(track)
            if recording_id is not None and position_key is not None:
                recording_ids_by_position[position_key].add(recording_id)
        if any(len(recording_ids) > 1 for recording_ids in recording_ids_by_position.values()):
            conflicts.append("conflicting MusicBrainz recording MBIDs at one album track position")
        identifier_warnings = tuple(
            dict.fromkeys(warning for track in members for warning in track.identifier_warnings)
        )
        per_file_conflicts = tuple(
            warning
            for warning in identifier_warnings
            if warning.startswith("conflicting ") and warning.endswith(" within one file")
        )
        conflicts.extend(per_file_conflicts)
        identifier_warnings = tuple(
            warning for warning in identifier_warnings if warning not in per_file_conflicts
        )
        groups.append(
            AlbumGroup(
                album=first.album,
                album_artist=first.album_artist or first.artist,
                year=first.year,
                files=tuple(sorted((track.path for track in members), key=str)),
                logical_tracks=tuple(logical.values()),
                barcode=barcode,
                musicbrainz_release_id=release_id,
                source_identities=tuple(
                    (track.path, track.source_identity)
                    for track in sorted(members, key=lambda item: str(item.path))
                    if track.source_identity is not None
                ),
                musicbrainz_provenance_complete=musicbrainz_provenance_complete,
                identifier_conflicts=tuple(conflicts),
                identifier_warnings=identifier_warnings,
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
