"""Album identity, edition, and verified-tracklist matching."""

from __future__ import annotations

import math
import re
import unicodedata
import uuid
from collections import defaultdict
from collections.abc import Iterable
from difflib import SequenceMatcher
from pathlib import Path

from .models import (
    AlbumGroup,
    CandidateScore,
    CatalogAlbum,
    CatalogTrack,
    MatchDecision,
    TrackMetadata,
)


def normalize_text(value: str) -> str:
    """Normalize catalog text without throwing away meaningful words."""
    value = unicodedata.normalize("NFKD", value).casefold()
    value = "".join(char for char in value if not unicodedata.combining(char))
    value = value.replace("&", " and ")
    value = re.sub(r"[^\w]+", " ", value, flags=re.UNICODE)
    return " ".join(value.split())


def text_similarity(left: str, right: str) -> float:
    left_n = normalize_text(left)
    right_n = normalize_text(right)
    if not left_n or not right_n:
        return 0.0
    if left_n == right_n:
        return 1.0
    char_score = SequenceMatcher(None, left_n, right_n).ratio()
    left_tokens = set(left_n.split())
    right_tokens = set(right_n.split())
    token_score = len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
    return 0.65 * char_score + 0.35 * token_score


def _artist_identity(value: str) -> str:
    """Normalize punctuation/case while preserving meaningful leading articles."""
    return normalize_text(value)


def _artists_equivalent(left: str, right: str) -> bool:
    left_identity = _artist_identity(left)
    right_identity = _artist_identity(right)
    if not left_identity or not right_identity:
        return False
    if left_identity == right_identity:
        return True
    if left_identity.startswith("the "):
        base = left_identity[4:]
        if base != "the" and base == right_identity:
            return True
    if right_identity.startswith("the "):
        base = right_identity[4:]
        if base != "the" and base == left_identity:
            return True
    return False


def _version_qualifiers(value: str) -> frozenset[str]:
    normalized = normalize_text(value)
    qualifiers: set[str] = set()
    patterns = {
        "deluxe": r"\bdeluxe(?: edition)?\b",
        "expanded": r"\bexpanded(?: edition)?\b",
        "anniversary": r"\banniversary(?: edition)?\b",
        "special": r"\bspecial edition\b",
        "collector": r"\bcollector(?: s)? edition\b",
        "extended": r"\bextended(?: edition| version)?\b",
        "soundtrack": r"\bsoundtrack\b",
        "live": r"\blive\b",
        "mono": r"\bmono\b",
        "stereo": r"\bstereo\b",
        "acoustic": r"\bacoustic\b",
        "instrumental": r"\binstrumental\b",
        "radio edit": r"\bradio edit\b",
        "drumless": r"\bdrumless\b",
        "demo": r"\bdemo\b",
        "bonus": r"\bbonus\b",
    }
    for name, pattern in patterns.items():
        if re.search(pattern, normalized):
            qualifiers.add(name)

    anniversary = re.search(r"\b(\d{1,3})(?:st|nd|rd|th)\s+anniversary\b", normalized)
    if anniversary:
        qualifiers.add(f"anniversary:{anniversary.group(1)}")

    remaster = re.search(
        r"\b(?:(18\d{2}|19\d{2}|20\d{2}|21\d{2})\s+)?(?:digital\s+)?"
        r"remaster(?:ed)?(?:\s+(18\d{2}|19\d{2}|20\d{2}|21\d{2}))?\b",
        normalized,
    )
    if remaster:
        qualifiers.add("remaster")
        remaster_year = remaster.group(1) or remaster.group(2)
        if remaster_year:
            qualifiers.add(f"remaster:{remaster_year}")
    remix = re.search(
        r"\b(?:(18\d{2}|19\d{2}|20\d{2}|21\d{2})\s+)?remix(?:ed)?"
        r"(?:\s+(18\d{2}|19\d{2}|20\d{2}|21\d{2}))?\b",
        normalized,
    )
    if remix:
        qualifiers.add("remix")
        remix_year = remix.group(1) or remix.group(2)
        if remix_year:
            qualifiers.add(f"remix:{remix_year}")
    return frozenset(qualifiers)


def _has_version_conflict(left: str, right: str) -> bool:
    return _version_qualifiers(left) != _version_qualifiers(right)


def _without_trailing_album_version(value: str) -> str:
    return re.sub(
        r"\s*[\[(]\s*album\s+version\s*[\])]\s*$",
        "",
        value,
        flags=re.IGNORECASE,
    )


def _title_similarity(left: str, right: str) -> float:
    def without_feature(value: str) -> str:
        normalized = normalize_text(value)
        return re.split(r"\b(?:feat|featuring|ft)\b", normalized, maxsplit=1)[0].strip()

    left_without_version = _without_trailing_album_version(left)
    right_without_version = _without_trailing_album_version(right)
    return max(
        text_similarity(left, right),
        text_similarity(without_feature(left), without_feature(right)),
        text_similarity(left_without_version, right_without_version),
        text_similarity(
            without_feature(left_without_version),
            without_feature(right_without_version),
        ),
    )


def _canonical_track_identity(value: str) -> str:
    value = _without_trailing_album_version(value)
    normalized = normalize_text(value)
    return re.split(r"\b(?:feat|featuring|ft)\b", normalized, maxsplit=1)[0].strip()


_TRAILING_REMASTER = re.compile(
    r"\s*[\[(]\s*(?P<label>"
    r"(?:(?:18|19|20|21)\d{2}\s+)?"
    r"(?:digital\s+)?remaster(?:ed)?"
    r"(?:\s+(?:18|19|20|21)\d{2})?"
    r")\s*[\])]\s*$",
    flags=re.IGNORECASE,
)


def _split_trailing_remaster(value: str) -> tuple[str, str] | None:
    match = _TRAILING_REMASTER.search(value)
    if match is None:
        return None
    return value[: match.start()].rstrip(), normalize_text(match.group("label"))


def _remaster_annotation_state(
    tracks: Iterable[TrackMetadata | CatalogTrack],
) -> tuple[str, str | None]:
    parsed = tuple(_split_trailing_remaster(track.title) for track in tracks)
    if parsed and all(item is None for item in parsed):
        return "absent", None
    if parsed and all(item is not None for item in parsed):
        labels = {item[1] for item in parsed if item is not None}
        if len(labels) == 1:
            return "uniform", next(iter(labels))
    return "mixed", None


def _complete_positions(
    tracks: Iterable[TrackMetadata | CatalogTrack],
) -> tuple[tuple[int, int], ...] | None:
    positions: list[tuple[int, int]] = []
    for track in tracks:
        number = track.track_number
        disc = track.disc_number or 1
        if number is None or number < 1 or disc < 1:
            return None
        positions.append((disc, number))
    if len(positions) != len(set(positions)):
        return None
    return tuple(sorted(positions))


_TRAILING_INSTRUMENTAL_ALBUM_VERSION = re.compile(
    r"\s*[\[(]\s*instrumental\s+album\s+version\s*[\])]\s*$",
    flags=re.IGNORECASE,
)


def _split_trailing_instrumental_album_version(value: str) -> str | None:
    match = _TRAILING_INSTRUMENTAL_ALBUM_VERSION.search(value)
    if match is None:
        return None
    return value[: match.start()].rstrip()


def _provider_omitted_instrumental_album_versions(
    local_tracks: tuple[TrackMetadata, ...],
    remote_tracks: tuple[CatalogTrack, ...],
) -> frozenset[tuple[int, int]]:
    if len(local_tracks) < 3 or len(local_tracks) != len(remote_tracks):
        return frozenset()
    local_positions = _complete_positions(local_tracks)
    remote_positions = _complete_positions(remote_tracks)
    if local_positions is None or local_positions != remote_positions:
        return frozenset()

    remote_by_position = {
        (track.disc_number or 1, track.track_number): track for track in remote_tracks
    }
    omitted_positions: set[tuple[int, int]] = set()
    for local in local_tracks:
        assert local.track_number is not None
        position = (local.disc_number or 1, local.track_number)
        remote = remote_by_position[position]
        local_base = _split_trailing_instrumental_album_version(local.title)
        if local_base is not None:
            if _version_qualifiers(remote.title):
                return frozenset()
            omitted_positions.add(position)
        else:
            local_base = _without_trailing_album_version(local.title)
        remote_base = _without_trailing_album_version(remote.title)
        if (
            normalize_text(local_base) != normalize_text(remote_base)
            or _has_version_conflict(local_base, remote_base)
            or local.duration_ms is None
            or remote.duration_ms is None
            or _duration_similarity(local.duration_ms, remote.duration_ms) == 0
        ):
            return frozenset()
    return frozenset(omitted_positions)


def _provider_omitted_uniform_remaster(
    local_tracks: tuple[TrackMetadata, ...],
    remote_tracks: tuple[CatalogTrack, ...],
) -> tuple[bool, bool]:
    if len(local_tracks) < 3 or len(local_tracks) != len(remote_tracks):
        return False, False
    local_positions = _complete_positions(local_tracks)
    remote_positions = _complete_positions(remote_tracks)
    if local_positions is None or local_positions != remote_positions:
        return False, False

    local_state, _local_label = _remaster_annotation_state(local_tracks)
    remote_state, _remote_label = _remaster_annotation_state(remote_tracks)
    if local_state != "uniform" or remote_state != "absent":
        return False, False

    remote_by_position = {
        (track.disc_number or 1, track.track_number): track for track in remote_tracks
    }
    for local in local_tracks:
        remote = remote_by_position[(local.disc_number or 1, local.track_number)]
        parsed_local = _split_trailing_remaster(local.title)
        assert parsed_local is not None
        local_title = parsed_local[0]
        remote_title = remote.title
        if (
            normalize_text(local_title) != normalize_text(remote_title)
            or _has_version_conflict(local_title, remote_title)
            or local.duration_ms is None
            or remote.duration_ms is None
            or _duration_similarity(local.duration_ms, remote.duration_ms) == 0
        ):
            return False, False
    return True, False


def _duration_similarity(left_ms: int | None, right_ms: int | None) -> float:
    if left_ms is None or right_ms is None:
        return 0.5
    difference = abs(left_ms - right_ms)
    tolerance = min(4_000.0, max(2_000.0, max(left_ms, right_ms) * 0.005))
    if difference > tolerance:
        return 0.0
    return 1.0 - 0.2 * (difference / tolerance)


_TRAILING_RELEASE_EDITION = re.compile(
    r"\s*[\[(]\s*(?:expanded|deluxe)(?:\s+edition)?\s*[\])]\s*$",
    flags=re.IGNORECASE,
)

_TRAILING_ALBUM_NICKNAME = re.compile(
    r"\s*[\[(]\s*(?:the\s+)?(?:[\w'-]+\s+){0,3}album\s*[\])]\s*$",
    flags=re.IGNORECASE,
)


def _strip_track_release_label(value: str) -> tuple[str, bool]:
    """Remove only trailing mastering labels that do not change a song's identity."""
    changed = False
    while True:
        without_album_version = _without_trailing_album_version(value)
        if without_album_version != value:
            value = without_album_version
            changed = True
            continue
        remaster = _split_trailing_remaster(value)
        if remaster is not None:
            value = remaster[0]
            changed = True
            continue
        return value, changed


def _strip_album_release_label(value: str) -> tuple[str, bool]:
    """Remove release packaging labels while preserving semantic edition words."""
    changed = False
    stripped = value.strip()
    if stripped.startswith("[") and stripped.endswith("]"):
        stripped = stripped[1:-1].strip()
        changed = True
    while True:
        without_album_version = _without_trailing_album_version(stripped)
        if without_album_version != stripped:
            stripped = without_album_version
            changed = True
            continue
        remaster = _split_trailing_remaster(stripped)
        if remaster is not None:
            stripped = remaster[0]
            changed = True
            continue
        without_edition = _TRAILING_RELEASE_EDITION.sub("", stripped).rstrip()
        if without_edition != stripped:
            stripped = without_edition
            changed = True
            continue
        return stripped, changed


def _semantic_qualifiers(value: str) -> frozenset[str]:
    return frozenset(
        qualifier
        for qualifier in _version_qualifiers(value)
        if qualifier not in {"remaster", "expanded", "deluxe"}
        and not qualifier.startswith("remaster:")
    )


def _explicit_remaster_years_conflict(left: str, right: str) -> bool:
    left_years = {
        qualifier for qualifier in _version_qualifiers(left) if qualifier.startswith("remaster:")
    }
    right_years = {
        qualifier for qualifier in _version_qualifiers(right) if qualifier.startswith("remaster:")
    }
    return bool(left_years and right_years and left_years != right_years)


def _release_album_names_related(left: str, right: str) -> tuple[bool, bool]:
    left_base, left_changed = _strip_album_release_label(left)
    right_base, right_changed = _strip_album_release_label(right)
    left_n = normalize_text(left_base)
    right_n = normalize_text(right_base)
    if not left_n or not right_n:
        return False, False
    related = left_n == right_n
    nickname_equivalent = False
    if not related:
        for titled, other in ((left_base, right_base), (right_base, left_base)):
            nickname = _TRAILING_ALBUM_NICKNAME.search(titled)
            if nickname is not None and normalize_text(
                titled[: nickname.start()]
            ) == normalize_text(other):
                related = True
                nickname_equivalent = True
                break
    return related, left_changed or right_changed or nickname_equivalent


def _aligned_release_label_positions(
    group: AlbumGroup,
    candidate: CatalogAlbum,
) -> frozenset[tuple[int, int]]:
    """Prove release identity before reconciling cross-provider mastering labels."""
    local_tracks = group.logical_tracks
    remote_tracks = candidate.tracks
    if (
        len(local_tracks) < 5
        or len(local_tracks) != len(remote_tracks)
        or candidate.track_count != len(remote_tracks)
        or _local_tracklist_incomplete(group)
        or not _artists_equivalent(group.album_artist, candidate.artist)
        or _semantic_qualifiers(group.album) != _semantic_qualifiers(candidate.album)
        or _explicit_remaster_years_conflict(group.album, candidate.album)
    ):
        return frozenset()
    local_positions = _complete_positions(local_tracks)
    remote_positions = _complete_positions(remote_tracks)
    if local_positions is None or local_positions != remote_positions:
        return frozenset()
    album_related, label_evidence = _release_album_names_related(group.album, candidate.album)
    if not album_related:
        return frozenset()

    remote_by_position = {
        (track.disc_number or 1, track.track_number): track for track in remote_tracks
    }
    compatible_durations = 0
    for local in local_tracks:
        assert local.track_number is not None
        position = (local.disc_number or 1, local.track_number)
        remote = remote_by_position[position]
        if not _artists_equivalent(local.artist, remote.artist):
            return frozenset()
        if _explicit_remaster_years_conflict(local.title, remote.title):
            return frozenset()
        local_base, local_changed = _strip_track_release_label(local.title)
        remote_base, remote_changed = _strip_track_release_label(remote.title)
        label_evidence = label_evidence or local_changed or remote_changed
        if normalize_text(local_base) != normalize_text(remote_base):
            return frozenset()
        if _semantic_qualifiers(local_base) != _semantic_qualifiers(remote_base):
            return frozenset()
        if local.duration_ms is None or remote.duration_ms is None:
            return frozenset()
        if _duration_similarity(local.duration_ms, remote.duration_ms) > 0:
            compatible_durations += 1
            continue
        difference = abs(local.duration_ms - remote.duration_ms)
        maximum_drift = max(10_000.0, max(local.duration_ms, remote.duration_ms) * 0.03)
        if difference > maximum_drift:
            return frozenset()
    if not label_evidence or compatible_durations < math.ceil(0.85 * len(local_tracks)):
        return frozenset()
    return frozenset(local_positions)


def _position_similarity(local: TrackMetadata, remote: CatalogTrack) -> float:
    known = 0
    matched = 0
    for left, right in (
        (local.disc_number, remote.disc_number),
        (local.track_number, remote.track_number),
    ):
        if left is not None and right is not None:
            known += 1
            matched += int(left == right)
    return matched / known if known else 0.5


def _match_tracks(
    local_tracks: tuple[TrackMetadata, ...],
    remote_tracks: tuple[CatalogTrack, ...],
    *,
    aligned_release_positions: frozenset[tuple[int, int]] = frozenset(),
) -> list[tuple[TrackMetadata, CatalogTrack, float, float, float]]:
    strip_local_remaster, strip_remote_remaster = _provider_omitted_uniform_remaster(
        local_tracks, remote_tracks
    )
    strip_local_instrumental_positions = _provider_omitted_instrumental_album_versions(
        local_tracks, remote_tracks
    )
    possible: list[tuple[float, TrackMetadata, CatalogTrack, float, float, float]] = []
    for local in local_tracks:
        for remote in remote_tracks:
            local_title = local.title
            remote_title = remote.title
            local_position = (local.disc_number or 1, local.track_number)
            remote_position = (remote.disc_number or 1, remote.track_number)
            aligned_release_pair = (
                local_position == remote_position and local_position in aligned_release_positions
            )
            if aligned_release_pair:
                local_title = _strip_track_release_label(local_title)[0]
                remote_title = _strip_track_release_label(remote_title)[0]
            if (
                local_position == remote_position
                and local_position in strip_local_instrumental_positions
            ):
                parsed_local_instrumental = _split_trailing_instrumental_album_version(local_title)
                assert parsed_local_instrumental is not None
                local_title = parsed_local_instrumental
            if strip_local_remaster and not aligned_release_pair:
                parsed_local = _split_trailing_remaster(local_title)
                assert parsed_local is not None
                local_title = parsed_local[0]
            if strip_remote_remaster and not aligned_release_pair:
                parsed_remote = _split_trailing_remaster(remote_title)
                assert parsed_remote is not None
                remote_title = parsed_remote[0]
            title_score = _title_similarity(local_title, remote_title)
            duration_score = _duration_similarity(local.duration_ms, remote.duration_ms)
            if local_position == remote_position and _canonical_track_identity(
                local_title
            ) != _canonical_track_identity(remote_title):
                continue
            if title_score < 0.93 or _has_version_conflict(local_title, remote_title):
                continue
            if (
                local.duration_ms is not None
                and remote.duration_ms is not None
                and duration_score == 0
                and not aligned_release_pair
            ):
                continue
            position_score = _position_similarity(local, remote)
            pair_score = 0.74 * title_score + 0.16 * duration_score + 0.10 * position_score
            possible.append(
                (
                    pair_score,
                    local,
                    remote,
                    title_score,
                    duration_score,
                    position_score,
                )
            )

    matches: list[tuple[TrackMetadata, CatalogTrack, float, float, float]] = []
    used_local: set[Path] = set()
    used_remote: set[tuple[int | None, int | None, str]] = set()
    for _pair_score, local, remote, title_score, duration_score, position_score in sorted(
        possible, key=lambda item: item[0], reverse=True
    ):
        remote_key = (remote.disc_number, remote.track_number, normalize_text(remote.title))
        if local.path in used_local or remote_key in used_remote:
            continue
        used_local.add(local.path)
        used_remote.add(remote_key)
        matches.append((local, remote, title_score, duration_score, position_score))
    return matches


def _local_tracklist_incomplete(group: AlbumGroup) -> bool:
    tracks = group.logical_tracks
    if not tracks:
        return True

    positions: dict[int, list[int]] = defaultdict(list)
    track_totals: dict[int, set[int]] = defaultdict(set)
    declared_disc_totals: set[int] = set()
    for track in tracks:
        disc = track.disc_number or 1
        number = track.track_number
        if disc < 1 or number is None or number < 1:
            return True
        positions[disc].append(number)
        if track.track_total is not None:
            if track.track_total < 1:
                return True
            track_totals[disc].add(track.track_total)
        if track.disc_total is not None:
            if track.disc_total < 1:
                return True
            declared_disc_totals.add(track.disc_total)

    observed_discs = set(positions)
    if observed_discs != set(range(1, max(observed_discs) + 1)):
        return True
    if len(declared_disc_totals) > 1:
        return True
    if declared_disc_totals and observed_discs != set(
        range(1, next(iter(declared_disc_totals)) + 1)
    ):
        return True

    all_declared_totals = {value for values in track_totals.values() for value in values}
    global_total_convention = len(all_declared_totals) == 1 and next(
        iter(all_declared_totals), 0
    ) == len(tracks)
    for disc, numbers in positions.items():
        if len(numbers) != len(set(numbers)):
            return True
        if set(numbers) != set(range(1, max(numbers) + 1)):
            return True
        declared = track_totals.get(disc, set())
        if len(declared) > 1:
            return True
        if declared and not global_total_convention and next(iter(declared)) != len(numbers):
            return True
    return False


def _normalize_barcode(value: str | None) -> str | None:
    if not value:
        return None
    compact = re.sub(r"[\s-]+", "", value)
    if not compact.isascii() or not compact.isdigit() or len(compact) not in {8, 12, 13, 14}:
        return None
    checksum = sum(
        int(digit) * (3 if (len(compact) - index) % 2 == 0 else 1)
        for index, digit in enumerate(compact)
    )
    return compact if checksum % 10 == 0 else None


def _normalize_release_id(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return str(uuid.UUID(value.strip()))
    except (ValueError, AttributeError):
        return None


def score_candidate(
    group: AlbumGroup,
    candidate: CatalogAlbum,
    *,
    allow_short_releases: bool = False,
) -> CandidateScore:
    """Score a candidate with hard identity and tracklist gates before fuzzy ranking."""
    aligned_release_positions = _aligned_release_label_positions(group, candidate)
    album_score = text_similarity(group.album, candidate.album)
    artist_score = text_similarity(group.album_artist, candidate.artist)
    matches = _match_tracks(
        group.logical_tracks,
        candidate.tracks,
        aligned_release_positions=aligned_release_positions,
    )
    local_count = len(group.logical_tracks)
    remote_song_count = len(candidate.tracks)
    remote_count = candidate.track_count or remote_song_count
    coverage = len(matches) / max(local_count, remote_song_count, remote_count, 1)
    track_title_score = sum(match[2] for match in matches) / len(matches) if matches else 0.0
    duration_score = sum(match[3] for match in matches) / len(matches) if matches else 0.0
    position_score = sum(match[4] for match in matches) / len(matches) if matches else 0.0
    track_artist_score = (
        sum(
            float(_artists_equivalent(local.artist, remote.artist)) for local, remote, *_ in matches
        )
        / len(matches)
        if matches
        else 0.0
    )
    count_score = (
        max(0.0, 1.0 - abs(local_count - remote_count) / max(local_count, remote_count, 1))
        if remote_count
        else 0.5
    )
    if group.year is None or candidate.release_year is None:
        year_score = 0.5
    else:
        year_score = max(0.0, 1.0 - abs(group.year - candidate.release_year) / 5)
    if aligned_release_positions:
        album_score = max(album_score, 0.95)
        year_score = max(year_score, 0.5)

    reasons: list[str] = []
    local_incomplete = _local_tracklist_incomplete(group)
    if local_incomplete:
        reasons.append("local tracklist appears incomplete")
    if remote_count != remote_song_count:
        reasons.append("Apple tracklist appears incomplete")
    local_positions = _complete_positions(group.logical_tracks)
    remote_positions = _complete_positions(candidate.tracks)
    if (
        not local_incomplete
        and local_positions is not None
        and remote_positions is not None
        and local_positions != remote_positions
    ):
        reasons.append("disc/track topology mismatch")
    if matches and any(match[4] < 1.0 for match in matches):
        reasons.append("track order mismatch")
    position_maps_equal = (
        not local_incomplete
        and local_positions is not None
        and remote_positions is not None
        and local_positions == remote_positions
    )
    if position_maps_equal and (
        len(matches) != local_count or any(match[4] < 1.0 for match in matches)
    ):
        reasons.append("positioned tracklist mismatch")
    if _has_version_conflict(group.album, candidate.album) and not aligned_release_positions:
        reasons.append("edition/version conflict")
    if album_score < 0.72:
        reasons.append("album mismatch")
    album_names_related, _album_label_evidence = _release_album_names_related(
        group.album, candidate.album
    )
    if not album_names_related:
        reasons.append("album title mismatch")
    if not _artists_equivalent(group.album_artist, candidate.artist):
        reasons.append("artist mismatch")
    if matches and track_artist_score < 1.0:
        reasons.append("track artist mismatch")
    required_matches = (
        1 if local_count == 1 else 2 if local_count == 2 else max(3, math.ceil(0.70 * local_count))
    )
    if len(matches) < required_matches:
        reasons.append("tracklist mismatch")
    local_barcode = _normalize_barcode(group.barcode)
    verified_barcode = _normalize_barcode(candidate.verified_barcode)
    identifier_verified = bool(local_barcode and local_barcode == verified_barcode)
    if local_barcode and verified_barcode and local_barcode != verified_barcode:
        reasons.append("barcode mismatch")
    if len(matches) < 3 and not identifier_verified and not allow_short_releases:
        reasons.append("fewer than three strong tracks")
    if coverage < 0.85:
        reasons.append("tracklist coverage below 85%")

    topology_score = 0.5 * count_score + 0.5 * position_score
    components = {
        "album": album_score,
        "artist": artist_score,
        "track_artist": track_artist_score,
        "track_coverage": coverage,
        "track_title": track_title_score,
        "duration": duration_score,
        "topology": topology_score,
        "track_count": count_score,
        "year": year_score,
        "position": position_score,
    }
    weights = {
        "album": 0.20,
        "artist": 0.15,
        "track_artist": 0.05,
        "track_title": 0.25,
        "duration": 0.20,
        "topology": 0.10,
        "year": 0.05,
    }
    total = sum(components[name] * weight for name, weight in weights.items())
    return CandidateScore(
        candidate=candidate,
        total=total,
        eligible=not reasons,
        reasons=tuple(reasons),
        components=components,
    )


def _equivalent_catalog_releases(left: CatalogAlbum, right: CatalogAlbum) -> bool:
    if (
        normalize_text(left.album) != normalize_text(right.album)
        or not _artists_equivalent(left.artist, right.artist)
        or left.release_year != right.release_year
        or left.track_count != right.track_count
        or _version_qualifiers(left.album) != _version_qualifiers(right.album)
    ):
        return False
    left_positions = _complete_positions(left.tracks)
    right_positions = _complete_positions(right.tracks)
    if left_positions is None or left_positions != right_positions:
        return False
    right_by_position = {
        (track.disc_number or 1, track.track_number): track for track in right.tracks
    }
    for left_track in left.tracks:
        position = (left_track.disc_number or 1, left_track.track_number)
        right_track = right_by_position[position]
        if (
            normalize_text(left_track.title) != normalize_text(right_track.title)
            or not _artists_equivalent(left_track.artist, right_track.artist)
            or _has_version_conflict(left_track.title, right_track.title)
            or left_track.duration_ms is None
            or right_track.duration_ms is None
            or _duration_similarity(left_track.duration_ms, right_track.duration_ms) == 0
        ):
            return False
    return True


def _aggregate_duration_distance(group: AlbumGroup, candidate: CatalogAlbum) -> int | None:
    local_positions = _complete_positions(group.logical_tracks)
    remote_positions = _complete_positions(candidate.tracks)
    if local_positions is None or local_positions != remote_positions:
        return None
    remote_by_position = {
        (track.disc_number or 1, track.track_number): track for track in candidate.tracks
    }
    distance = 0
    for local in group.logical_tracks:
        assert local.track_number is not None
        remote = remote_by_position[(local.disc_number or 1, local.track_number)]
        if local.duration_ms is None or remote.duration_ms is None:
            return None
        distance += abs(local.duration_ms - remote.duration_ms)
    return distance


def _duration_fingerprint_breaks_tie(
    group: AlbumGroup,
    best: CandidateScore,
    runner: CandidateScore,
) -> bool:
    if not _equivalent_catalog_releases(best.candidate, runner.candidate):
        return False
    best_distance = _aggregate_duration_distance(group, best.candidate)
    runner_distance = _aggregate_duration_distance(group, runner.candidate)
    if best_distance is None or runner_distance is None:
        return False
    return best_distance + 100 <= runner_distance and best_distance <= runner_distance * 0.90


def choose_match(
    group: AlbumGroup,
    candidates: Iterable[CatalogAlbum],
    *,
    min_score: float = 0.92,
    min_margin: float = 0.10,
    allow_short_releases: bool = False,
) -> MatchDecision:
    scores = tuple(
        sorted(
            (
                score_candidate(
                    group,
                    candidate,
                    allow_short_releases=allow_short_releases,
                )
                for candidate in candidates
            ),
            key=lambda score: score.total,
            reverse=True,
        )
    )
    eligible = [score for score in scores if score.eligible]
    if not eligible:
        return MatchDecision(
            "no_match", None, scores, "no candidate passed identity and tracklist gates"
        )
    local_barcode = _normalize_barcode(group.barcode)
    if local_barcode is not None:
        identifier_verified = [
            score
            for score in eligible
            if _normalize_barcode(score.candidate.verified_barcode) == local_barcode
        ]
        if identifier_verified:
            eligible = identifier_verified
    best = eligible[0]
    if best.total < min_score:
        return MatchDecision(
            "low_confidence", None, scores, f"best score {best.total:.3f} is below {min_score:.3f}"
        )
    if len(eligible) > 1:
        close_runners = [
            runner for runner in eligible[1:] if best.total - runner.total < min_margin
        ]
        if close_runners and not all(
            _duration_fingerprint_breaks_tie(group, best, runner) for runner in close_runners
        ):
            margin = best.total - eligible[1].total
            return MatchDecision(
                "ambiguous",
                None,
                scores,
                f"top-candidate margin {margin:.3f} is below {min_margin:.3f}",
            )
    return MatchDecision("matched", best, scores, "verified metadata and tracklist match")


__all__ = (
    "choose_match",
    "normalize_text",
    "score_candidate",
    "text_similarity",
)
