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


def _title_similarity(left: str, right: str) -> float:
    def without_feature(value: str) -> str:
        normalized = normalize_text(value)
        return re.split(r"\b(?:feat|featuring|ft)\b", normalized, maxsplit=1)[0].strip()

    def without_album_version(value: str) -> str:
        return re.sub(
            r"\s*[\[(]\s*album\s+version\s*[\])]\s*$",
            "",
            value,
            flags=re.IGNORECASE,
        )

    left_without_version = without_album_version(left)
    right_without_version = without_album_version(right)
    return max(
        text_similarity(left, right),
        text_similarity(without_feature(left), without_feature(right)),
        text_similarity(left_without_version, right_without_version),
        text_similarity(
            without_feature(left_without_version),
            without_feature(right_without_version),
        ),
    )


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
    local_tracks: tuple[TrackMetadata, ...], remote_tracks: tuple[CatalogTrack, ...]
) -> list[tuple[TrackMetadata, CatalogTrack, float, float, float]]:
    strip_local_remaster, strip_remote_remaster = _provider_omitted_uniform_remaster(
        local_tracks, remote_tracks
    )
    possible: list[tuple[float, TrackMetadata, CatalogTrack, float, float, float]] = []
    for local in local_tracks:
        for remote in remote_tracks:
            local_title = local.title
            remote_title = remote.title
            if strip_local_remaster:
                parsed_local = _split_trailing_remaster(local_title)
                assert parsed_local is not None
                local_title = parsed_local[0]
            if strip_remote_remaster:
                parsed_remote = _split_trailing_remaster(remote_title)
                assert parsed_remote is not None
                remote_title = parsed_remote[0]
            title_score = _title_similarity(local_title, remote_title)
            duration_score = _duration_similarity(local.duration_ms, remote.duration_ms)
            if title_score < 0.93 or _has_version_conflict(local_title, remote_title):
                continue
            if (
                local.duration_ms is not None
                and remote.duration_ms is not None
                and duration_score == 0
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
    album_score = text_similarity(group.album, candidate.album)
    artist_score = text_similarity(group.album_artist, candidate.artist)
    matches = _match_tracks(group.logical_tracks, candidate.tracks)
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

    reasons: list[str] = []
    local_incomplete = _local_tracklist_incomplete(group)
    if local_incomplete:
        reasons.append("local tracklist appears incomplete")
    if remote_count != remote_song_count:
        reasons.append("Apple tracklist appears incomplete")
    local_positions = tuple(
        sorted(
            (track.disc_number, track.track_number)
            for track in group.logical_tracks
            if track.disc_number is not None and track.track_number is not None
        )
    )
    remote_positions = tuple(
        sorted(
            (track.disc_number, track.track_number)
            for track in candidate.tracks
            if track.disc_number is not None and track.track_number is not None
        )
    )
    if (
        not local_incomplete
        and len(local_positions) == local_count
        and len(remote_positions) == remote_song_count
        and local_positions != remote_positions
    ):
        reasons.append("disc/track topology mismatch")
    if _has_version_conflict(group.album, candidate.album):
        reasons.append("edition/version conflict")
    if album_score < 0.72:
        reasons.append("album mismatch")
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
    best = eligible[0]
    if best.total < min_score:
        return MatchDecision(
            "low_confidence", None, scores, f"best score {best.total:.3f} is below {min_score:.3f}"
        )
    if len(eligible) > 1:
        margin = best.total - eligible[1].total
        if margin < min_margin:
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
