"""Album identity, edition, and verified-tracklist matching."""

from __future__ import annotations

import math
import re
import unicodedata
import uuid
from collections import defaultdict
from collections.abc import Iterable, Mapping
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


def _artist_credit_extends_base(credit: str, base: str) -> bool:
    credit_identity = _artist_identity(credit)
    base_identity = _artist_identity(base)
    if not credit_identity or not base_identity:
        return False
    if credit_identity == base_identity:
        return True
    credit_folded = unicodedata.normalize("NFKD", credit).casefold().strip()
    base_folded = unicodedata.normalize("NFKD", base).casefold().strip()
    if credit_folded.startswith(base_folded):
        suffix = credit_folded[len(base_folded) :]
        if re.match(
            r"^\s*(?:,|&|\b(?:and|feat(?:uring)?|ft|with|x)\b)",
            suffix,
        ):
            return True
    return any(
        credit_identity.startswith(f"{base_identity} {separator} ")
        for separator in ("and", "feat", "featuring", "ft", "with", "x")
    )


def _musicbrainz_album_artists_compatible(left: str, right: str) -> bool:
    return (
        _artists_equivalent(left, right)
        or _artist_credit_extends_base(left, right)
        or _artist_credit_extends_base(right, left)
    )


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


_EXPLICIT_FEATURE_MARKER = r"(?:(?:feat(?:uring)?|ft)\.?|w/)"
_BRACKETED_FEATURE_MARKER = rf"(?:{_EXPLICIT_FEATURE_MARKER}|with)"
_BRACKETED_FEATURE_START = re.compile(
    rf"[\[(]\s*{_BRACKETED_FEATURE_MARKER}\s+",
    flags=re.IGNORECASE,
)
_FEATURE_QUALIFIER_TEXT = (
    r"(?:live|mono|stereo|acoustic|instrumental|radio\s+edit|demo|"
    r"(?:[^\[\]()]{1,100}\s+)?remix|"
    r"(?:digital\s+)?remaster(?:ed)?|album\s+version)"
    r"(?:\s+(?:18|19|20|21)\d{2})?|"
    r"(?:18|19|20|21)\d{2}\s+(?:digital\s+)?remaster(?:ed)?|"
    r"bonus(?:\s+tracks?)?|explicit|clean"
)
_TRAILING_FEATURE_QUALIFIER = re.compile(
    rf"\s*[-\u2010-\u2015]\s*(?P<label>{_FEATURE_QUALIFIER_TEXT})\s*$",
    flags=re.IGNORECASE,
)
_TRAILING_BRACKETED_FEATURE_QUALIFIER = re.compile(
    rf"\s*[\[(]\s*{_FEATURE_QUALIFIER_TEXT}\s*[\])]\s*$",
    flags=re.IGNORECASE,
)
_TRAILING_SQUARE_PRESENTATION = re.compile(r"\s*\[[^\[\]]+\]\s*$")
_UNBRACKETED_FEATURE_START = re.compile(
    rf"(?<!\w){_EXPLICIT_FEATURE_MARKER}\s+",
    flags=re.IGNORECASE,
)


def _bracketed_feature_credits(
    value: str,
) -> tuple[tuple[int, int, str, str | None], ...]:
    """Return balanced bracketed explicit credits; ignore malformed unbalanced spans."""
    if len(value) > 4096:
        return ()
    credits: list[tuple[int, int, str, str | None]] = []
    for match in _BRACKETED_FEATURE_START.finditer(value):
        opener_index = match.start()
        if any(start <= opener_index < end for start, end, _content, _qualifier in credits):
            continue
        opener = value[opener_index]
        closer = "]" if opener == "[" else ")"
        stack = [closer]
        cursor = match.end()
        while cursor < len(value) and stack:
            char = value[cursor]
            if char == "[":
                stack.append("]")
            elif char == "(":
                stack.append(")")
            elif char in "])":
                if char != stack[-1]:
                    break
                stack.pop()
            cursor += 1
        if stack:
            continue
        content = value[match.end() : cursor - 1].strip()
        qualifier_match = _TRAILING_FEATURE_QUALIFIER.search(content)
        qualifier = qualifier_match.group("label") if qualifier_match else None
        if qualifier_match:
            content = content[: qualifier_match.start()].strip()
        if content:
            credits.append((opener_index, cursor, content, qualifier))
    return tuple(credits)


def _inside_unclosed_bracket(value: str, position: int) -> bool:
    stack: list[str] = []
    for char in value[:position]:
        if char == "[":
            stack.append("]")
        elif char == "(":
            stack.append(")")
        elif char in "])":
            if not stack or stack[-1] != char:
                return True
            stack.pop()
    return bool(stack)


def _unbracketed_feature_credits(
    value: str,
    bracketed_spans: tuple[tuple[int, int], ...],
) -> tuple[tuple[int, int, str, str | None], ...]:
    if len(value) > 4096:
        return ()
    credits: list[tuple[int, int, str, str | None]] = []
    for match in _UNBRACKETED_FEATURE_START.finditer(value):
        if any(
            start <= match.start() < end for start, end in bracketed_spans
        ) or _inside_unclosed_bracket(value, match.start()):
            continue
        content_end = len(value)
        span_end = len(value)
        qualifier: str | None = None
        tail = value[match.end() :]
        qualifier_match = _TRAILING_FEATURE_QUALIFIER.search(tail)
        if qualifier_match:
            content_end = match.end() + qualifier_match.start()
            qualifier = qualifier_match.group("label")
        else:
            presentation_match = _TRAILING_BRACKETED_FEATURE_QUALIFIER.search(tail)
            square_match = _TRAILING_SQUARE_PRESENTATION.search(tail)
            boundary = presentation_match or square_match
            if boundary:
                content_end = match.end() + boundary.start()
                span_end = content_end
        content = value[match.end() : content_end].strip()
        if content:
            credits.append((match.start(), span_end, content, qualifier))
    return tuple(credits)


def _explicit_feature_credits(
    value: str,
) -> tuple[tuple[int, int, str, str | None], ...]:
    bracketed = _bracketed_feature_credits(value)
    bracketed_spans = tuple((start, end) for start, end, _content, _qualifier in bracketed)
    return bracketed + _unbracketed_feature_credits(value, bracketed_spans)


def _without_feature_credit(value: str) -> str:
    """Remove a feature-credit annotation without dropping later title qualifiers."""
    for start, end, _content, qualifier in sorted(
        _explicit_feature_credits(value),
        reverse=True,
    ):
        prefix_start = start
        while prefix_start > 0 and value[prefix_start - 1].isspace():
            prefix_start -= 1
        replacement = f" [{qualifier}]" if qualifier else ""
        value = value[:prefix_start] + replacement + value[end:]
    return value.strip()


def _collaborator_identities(value: str) -> frozenset[str]:
    separated = unicodedata.normalize("NFKD", value).casefold()
    separated = re.sub(
        rf"\s+{_EXPLICIT_FEATURE_MARKER}\s+",
        "\0",
        separated,
        flags=re.IGNORECASE,
    )
    separated = re.sub(r"\s*(?:,|;|&|\+)\s*", "\0", separated)
    separated = re.sub(r"\s+\b(?:and|with|x)\b\s+", "\0", separated)
    return frozenset(
        identity for part in separated.split("\0") if (identity := normalize_text(part))
    )


def _feature_credit_identities(*values: str) -> frozenset[str]:
    identities: set[str] = set()
    for value in values:
        value = _TRAILING_APPLE_RELEASE_TYPE.sub("", value).rstrip()
        for _start, _end, content, _qualifier in _explicit_feature_credits(value):
            identities.update(_collaborator_identities(content))
    return frozenset(identities)


_UNMARKED_ARTIST_COLLABORATOR_DELIMITER = re.compile(
    r"\s*[,;&+]\s*|\s+(?:and|x)\s+",
    flags=re.IGNORECASE,
)


def _unmarked_artist_suffix_matches_explicit_features(
    primary_artist: str,
    candidate_artist: str,
    explicit_features: frozenset[str],
) -> bool:
    """Reconcile a known feature set with one delimiter-marked Apple artist suffix."""
    if not explicit_features:
        return False
    for delimiter in _UNMARKED_ARTIST_COLLABORATOR_DELIMITER.finditer(candidate_artist):
        candidate_base = candidate_artist[: delimiter.start()].strip()
        candidate_suffix = candidate_artist[delimiter.end() :].strip()
        if (
            candidate_base
            and candidate_suffix
            and _artists_equivalent(primary_artist, candidate_base)
            and _collaborator_identities(candidate_suffix) == explicit_features
        ):
            return True
    return False


def _musicbrainz_search_artist_and_features_match(
    title: str,
    artist: str,
    candidate_title: str,
    candidate_artist: str,
) -> bool:
    left = _feature_credit_identities(title, artist)
    right = _feature_credit_identities(candidate_title, candidate_artist)
    left_base = _without_feature_credit(artist)
    right_base = _without_feature_credit(candidate_artist)
    if _artists_equivalent(left_base, right_base) and left == right:
        return True
    if (
        left
        and left == right
        and _unmarked_artist_suffix_matches_explicit_features(
            left_base,
            candidate_artist,
            left,
        )
    ):
        return True
    return bool(
        left
        and not right
        and _unmarked_artist_suffix_matches_explicit_features(
            left_base,
            candidate_artist,
            left,
        )
    )


def _normalize_colloquial_ing(value: str) -> str:
    return re.sub(
        r"\b([^\W\d_]+)in[\u2019'](?=$|[^\w])",
        r"\1ing",
        value,
        flags=re.IGNORECASE,
    )


def _title_similarity(left: str, right: str) -> float:
    left_without_version = _without_trailing_album_version(left)
    right_without_version = _without_trailing_album_version(right)
    left_without_feature = _without_feature_credit(left)
    right_without_feature = _without_feature_credit(right)
    left_canonical_style = _normalize_colloquial_ing(left_without_feature)
    right_canonical_style = _normalize_colloquial_ing(right_without_feature)
    return max(
        text_similarity(left, right),
        text_similarity(left_without_feature, right_without_feature),
        text_similarity(left_without_version, right_without_version),
        text_similarity(
            _without_feature_credit(left_without_version),
            _without_feature_credit(right_without_version),
        ),
        text_similarity(left_canonical_style, right_canonical_style),
    )


def _canonical_track_identity(value: str) -> str:
    value = _without_trailing_album_version(value)
    value = _without_feature_credit(value)
    value = _normalize_colloquial_ing(value)
    return normalize_text(value)


def _musicbrainz_track_identity(value: str) -> str:
    """Normalize a provider's redundant ``Acoustic Version``-style wording."""
    identity = _canonical_track_identity(value)
    return re.sub(
        r"\b(acoustic|instrumental|live|mono|stereo|demo|radio edit|remix) version$",
        r"\1",
        identity,
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

_TRAILING_APPLE_RELEASE_TYPE = re.compile(
    r"\s*[-\u2010-\u2015]\s*(?:single|ep)\s*$",
    flags=re.IGNORECASE,
)

_TRAILING_IDENTIFIER_PRESENTATION = re.compile(
    r"\s*[\[(]\s*"
    r"(?:(?:expanded|deluxe)(?:\s+edition)?|explicit|clean|"
    r"bonus(?:\s+tracks?)?(?:\s+version)?|album\s+version)"
    r"(?:\s*[-\u2010-\u2015,/&+]\s*(?:(?:expanded|deluxe)(?:\s+edition)?|explicit|clean|"
    r"bonus(?:\s+tracks?)?(?:\s+version)?|album\s+version))*"
    r"\s*[\])]\s*$",
    flags=re.IGNORECASE,
)

_TRAILING_DASH_IDENTIFIER_PRESENTATION = re.compile(
    r"\s*[-\u2010-\u2015]\s*"
    r"(?:(?:expanded|deluxe)(?:\s+edition)?|explicit|clean|"
    r"bonus(?:\s+tracks?)?(?:\s+version)?|album\s+version)\s*$",
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


def _musicbrainz_album_identity(value: str) -> str:
    value = _TRAILING_APPLE_RELEASE_TYPE.sub("", value).rstrip()
    while True:
        stripped = _TRAILING_IDENTIFIER_PRESENTATION.sub("", value).rstrip()
        stripped = _TRAILING_DASH_IDENTIFIER_PRESENTATION.sub("", stripped).rstrip()
        remaster = _split_trailing_remaster(stripped)
        if remaster is not None:
            stripped = remaster[0]
        else:
            stripped = re.sub(
                r"\s*[-\u2010-\u2015]\s*"
                r"(?:(?:18|19|20|21)\d{2}\s+)?(?:digital\s+)?"
                r"remaster(?:ed)?(?:\s+(?:18|19|20|21)\d{2})?\s*$",
                "",
                stripped,
                flags=re.IGNORECASE,
            ).rstrip()
        if stripped == value:
            break
        value = stripped
    value = _without_feature_credit(value)
    value = _strip_album_release_label(value)[0]
    normalized = normalize_text(value)
    return re.sub(r"\bvolume\b", "vol", normalized)


def _duplicate_track_title_identity(value: str) -> str:
    """Normalize only provider presentation that cannot distinguish duplicate encodes."""
    value = _without_feature_credit(value)
    value = _strip_track_release_label(value)[0]
    presentation = re.compile(
        r"\s*[\[(]\s*(?:explicit|clean|bonus(?:\s+tracks?)?)\s*[\])]\s*$",
        flags=re.IGNORECASE,
    )
    while True:
        stripped = presentation.sub("", value).rstrip()
        if stripped == value:
            return normalize_text(value)
        value = stripped


def _duplicate_track_presentations_compatible(
    left: TrackMetadata,
    right: TrackMetadata,
) -> bool:
    if _duplicate_track_title_identity(left.title) != _duplicate_track_title_identity(right.title):
        return False
    left_features = _feature_credit_identities(left.title, left.artist)
    right_features = _feature_credit_identities(right.title, right.artist)
    return not left_features or not right_features or left_features == right_features


def _musicbrainz_search_identity_matches(
    title: str,
    artist: str,
    track_count: int | None,
    candidate_title: str,
    candidate_artist: str,
    candidate_track_count: int | None,
    release_year: int | None = None,
    candidate_release_year: int | None = None,
) -> bool:
    """Require an inferred Apple search result to preserve resolved MB identity."""
    title_without_features = _without_feature_credit(title)
    candidate_title_without_features = _without_feature_credit(candidate_title)
    if (
        _musicbrainz_album_identity(title) != _musicbrainz_album_identity(candidate_title)
        or not _musicbrainz_search_artist_and_features_match(
            title,
            artist,
            candidate_title,
            candidate_artist,
        )
        or (_semantic_qualifiers(title_without_features) - {"bonus"})
        != (_semantic_qualifiers(candidate_title_without_features) - {"bonus"})
        or _explicit_remaster_years_conflict(
            title_without_features,
            candidate_title_without_features,
        )
    ):
        return False
    return bool(
        track_count is not None
        and candidate_track_count is not None
        and track_count == candidate_track_count
        and not (
            release_year is not None
            and candidate_release_year is not None
            and abs(release_year - candidate_release_year) > 1
        )
    )


def _has_complete_musicbrainz_provenance(group: AlbumGroup) -> bool:
    release_id = _normalize_release_id(group.musicbrainz_release_id)
    return bool(
        group.musicbrainz_provenance_complete
        and release_id
        and group.logical_tracks
        and all(
            _normalize_release_id(track.musicbrainz_release_id) == release_id
            and _normalize_release_id(track.musicbrainz_recording_id) is not None
            for track in group.logical_tracks
        )
    )


def _musicbrainz_track_artists_compatible(
    group: AlbumGroup,
    candidate: CatalogAlbum,
    local: TrackMetadata,
    remote: CatalogTrack,
) -> bool:
    if _artists_equivalent(local.artist, remote.artist):
        return True
    if not _musicbrainz_album_artists_compatible(group.album_artist, candidate.artist):
        return False
    return _artist_credit_extends_base(local.artist, group.album_artist) and (
        _artist_credit_extends_base(remote.artist, candidate.artist)
    )


def _positions_are_sequential(positions: tuple[tuple[int, int], ...]) -> bool:
    by_disc: dict[int, list[int]] = defaultdict(list)
    for disc, number in positions:
        by_disc[disc].append(number)
    return set(by_disc) == set(range(1, max(by_disc, default=0) + 1)) and all(
        sorted(numbers) == list(range(1, len(numbers) + 1)) for numbers in by_disc.values()
    )


def _trusted_musicbrainz_alignment(
    group: AlbumGroup,
    candidate: CatalogAlbum,
) -> Mapping[tuple[int, int], tuple[int, int]]:
    """Prove an Apple release against a completely Picard-identified local release."""
    local_tracks = group.logical_tracks
    remote_tracks = candidate.tracks
    if (
        not _has_complete_musicbrainz_provenance(group)
        or len(local_tracks) != len(remote_tracks)
        or candidate.track_count != len(remote_tracks)
        or _local_tracklist_incomplete(group)
        or not _musicbrainz_album_artists_compatible(group.album_artist, candidate.artist)
        or _musicbrainz_album_identity(group.album) != _musicbrainz_album_identity(candidate.album)
        or _semantic_qualifiers(group.album) != _semantic_qualifiers(candidate.album)
        or _explicit_remaster_years_conflict(group.album, candidate.album)
    ):
        return {}
    local_positions = _complete_positions(local_tracks)
    remote_positions = _complete_positions(remote_tracks)
    if local_positions is None or remote_positions is None:
        return {}
    if local_positions == remote_positions:
        alignment = {position: position for position in local_positions}
    else:
        local_barcode = _normalize_barcode(group.barcode)
        verified_barcode = _normalize_barcode(candidate.verified_barcode)
        local_discs = {disc for disc, _number in local_positions}
        remote_discs = {disc for disc, _number in remote_positions}
        if (
            local_barcode is None
            or not _barcodes_equivalent(local_barcode, verified_barcode)
            or not _positions_are_sequential(local_positions)
            or not _positions_are_sequential(remote_positions)
            or (len(local_discs) == 1) == (len(remote_discs) == 1)
        ):
            return {}
        alignment = dict(zip(local_positions, remote_positions, strict=True))

    local_by_position = {
        (track.disc_number or 1, track.track_number): track for track in local_tracks
    }
    remote_by_position = {
        (track.disc_number or 1, track.track_number): track for track in remote_tracks
    }
    compatible_durations = 0
    for local_position, remote_position in alignment.items():
        local = local_by_position[local_position]
        remote = remote_by_position[remote_position]
        if (
            _musicbrainz_track_identity(local.title) != _musicbrainz_track_identity(remote.title)
            or _version_qualifiers(local.title) != _version_qualifiers(remote.title)
            or not _musicbrainz_track_artists_compatible(group, candidate, local, remote)
            or local.duration_ms is None
            or remote.duration_ms is None
        ):
            return {}
        if _duration_similarity(local.duration_ms, remote.duration_ms) > 0:
            compatible_durations += 1
            continue
        difference = abs(local.duration_ms - remote.duration_ms)
        maximum_drift = max(10_000.0, max(local.duration_ms, remote.duration_ms) * 0.03)
        if difference > maximum_drift:
            return {}
    if compatible_durations < math.ceil(0.85 * len(local_tracks)):
        return {}
    return alignment


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
    trusted_musicbrainz_alignment: Mapping[tuple[int, int], tuple[int, int]] | None = None,
) -> list[tuple[TrackMetadata, CatalogTrack, float, float, float]]:
    musicbrainz_alignment = trusted_musicbrainz_alignment or {}
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
            trusted_musicbrainz_pair = musicbrainz_alignment.get(local_position) == remote_position
            if musicbrainz_alignment and not trusted_musicbrainz_pair:
                continue
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
            if trusted_musicbrainz_pair and _musicbrainz_track_identity(
                local_title
            ) == _musicbrainz_track_identity(remote_title):
                title_score = 1.0
            duration_score = _duration_similarity(local.duration_ms, remote.duration_ms)
            if (
                local_position == remote_position
                and not trusted_musicbrainz_pair
                and _canonical_track_identity(local_title)
                != _canonical_track_identity(remote_title)
            ):
                continue
            if title_score < 0.93 or _has_version_conflict(local_title, remote_title):
                continue
            if (
                local.duration_ms is not None
                and remote.duration_ms is not None
                and duration_score == 0
                and not aligned_release_pair
                and not trusted_musicbrainz_pair
            ):
                continue
            position_score = (
                1.0 if trusted_musicbrainz_pair else _position_similarity(local, remote)
            )
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
    return compact if checksum % 10 == 0 and int(compact) != 0 else None


def _barcode_equivalence_key(value: str | None) -> str | None:
    normalized = _normalize_barcode(value)
    return normalized.zfill(14) if normalized is not None else None


def _barcodes_equivalent(left: str | None, right: str | None) -> bool:
    left_key = _barcode_equivalence_key(left)
    return left_key is not None and left_key == _barcode_equivalence_key(right)


def _normalize_release_id(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = uuid.UUID(value.strip())
        return str(parsed) if parsed.int != 0 else None
    except (ValueError, AttributeError):
        return None


def matching_basis(group: AlbumGroup) -> str:
    """Return the v3 matching path selected by valid embedded identifiers."""
    sources: list[str] = []
    if _normalize_release_id(group.musicbrainz_release_id) is not None:
        sources.append("musicbrainz")
    if _normalize_barcode(group.barcode) is not None:
        sources.append("upc")
    return "+".join(sources) if sources else "legacy"


def _identifier_duration_evidence(
    local_tracks: tuple[TrackMetadata, ...],
    remote_tracks: tuple[CatalogTrack, ...],
) -> tuple[float, float, int]:
    """Compare duration fingerprints as multisets so provider order is irrelevant."""
    local_durations = sorted(
        track.duration_ms for track in local_tracks if track.duration_ms is not None
    )
    remote_durations = sorted(
        track.duration_ms for track in remote_tracks if track.duration_ms is not None
    )
    known_pairs = min(len(local_durations), len(remote_durations))
    denominator = max(len(local_tracks), len(remote_tracks), 1)
    if known_pairs == 0:
        return 0.5, 0.0, 0

    compatible = 0
    closeness = 0.0
    aggregate_distance = 0
    for local_duration, remote_duration in zip(
        local_durations[:known_pairs],
        remote_durations[:known_pairs],
        strict=True,
    ):
        difference = abs(local_duration - remote_duration)
        tolerance = max(10_000.0, max(local_duration, remote_duration) * 0.03)
        aggregate_distance += difference
        if difference <= tolerance:
            compatible += 1
            closeness += 1.0 - 0.15 * (difference / tolerance)
    return compatible / denominator, closeness / denominator, aggregate_distance


def _identifier_presentation_warnings(
    local_tracks: tuple[TrackMetadata, ...],
    remote_tracks: tuple[CatalogTrack, ...],
    *,
    duration_coverage: float,
) -> list[str]:
    warnings: list[str] = []
    local_positions = _complete_positions(local_tracks)
    remote_positions = _complete_positions(remote_tracks)
    if (
        local_positions is not None
        and remote_positions is not None
        and local_positions != remote_positions
    ):
        warnings.append("disc/track topology difference ignored by identifier-first matching")

    if len(local_tracks) != len(remote_tracks) or duration_coverage < 0.70:
        return warnings
    ordered_local = sorted(
        local_tracks,
        key=lambda track: (track.disc_number or 1, track.track_number or 0),
    )
    ordered_remote = sorted(
        remote_tracks,
        key=lambda track: (track.disc_number or 1, track.track_number or 0),
    )
    known_pairs = 0
    position_matches = 0
    for local, remote in zip(ordered_local, ordered_remote, strict=True):
        if local.duration_ms is None or remote.duration_ms is None:
            continue
        known_pairs += 1
        if _duration_similarity(local.duration_ms, remote.duration_ms) > 0:
            position_matches += 1
    if known_pairs and position_matches / known_pairs + 0.20 < duration_coverage:
        warnings.append("track order difference ignored by identifier-first matching")
    return warnings


def _score_identifier_candidate(
    group: AlbumGroup,
    candidate: CatalogAlbum,
) -> CandidateScore:
    """Score Apple candidates using v3's identifier-first, order-agnostic policy."""
    basis = matching_basis(group)
    local_barcode = _normalize_barcode(group.barcode)
    verified_barcode = _normalize_barcode(candidate.verified_barcode)
    release_id = _normalize_release_id(group.musicbrainz_release_id)
    candidate_release_id = _normalize_release_id(candidate.verified_musicbrainz_release_id)
    verified_upc = _barcodes_equivalent(local_barcode, verified_barcode)
    conflicting_upc = bool(
        local_barcode
        and verified_barcode
        and not _barcodes_equivalent(local_barcode, verified_barcode)
    )
    musicbrainz_release = release_id is not None
    verified_musicbrainz = bool(release_id and release_id == candidate_release_id)
    conflicting_musicbrainz = bool(
        release_id and candidate_release_id and release_id != candidate_release_id
    )
    musicbrainz_search = bool(
        verified_musicbrainz
        and candidate.identifier_resolution == "musicbrainz_search"
        and candidate.resolved_musicbrainz_title
        and candidate.resolved_musicbrainz_artist
        and _musicbrainz_search_identity_matches(
            candidate.resolved_musicbrainz_title,
            candidate.resolved_musicbrainz_artist,
            candidate.musicbrainz_search_track_count,
            candidate.album,
            candidate.artist,
            candidate.track_count,
            candidate.resolved_musicbrainz_release_year,
            candidate.release_year,
        )
    )
    direct_musicbrainz = bool(
        verified_musicbrainz
        and candidate.identifier_resolution in {"musicbrainz_apple_relation", "musicbrainz_barcode"}
    )
    direct_identifier = verified_upc or direct_musicbrainz
    identifier_provenance = direct_identifier or musicbrainz_search
    musicbrainz_complete = bool(musicbrainz_release and _has_complete_musicbrainz_provenance(group))

    local_count = len(group.logical_tracks)
    remote_song_count = len(candidate.tracks)
    remote_count = candidate.track_count or remote_song_count
    count_score = (
        max(0.0, 1.0 - abs(local_count - remote_count) / max(local_count, remote_count, 1))
        if remote_count
        else 0.0
    )
    duration_coverage, duration_score, aggregate_distance = _identifier_duration_evidence(
        group.logical_tracks,
        candidate.tracks,
    )
    album_score = text_similarity(group.album, candidate.album)
    artist_score = text_similarity(group.album_artist, candidate.artist)
    album_identity_equal = _musicbrainz_album_identity(group.album) == _musicbrainz_album_identity(
        candidate.album
    )
    artist_compatible = _musicbrainz_album_artists_compatible(
        group.album_artist,
        candidate.artist,
    )
    if group.year is None or candidate.release_year is None:
        year_score = 0.5
    else:
        year_score = max(0.0, 1.0 - abs(group.year - candidate.release_year) / 10)

    reasons: list[str] = []
    warnings: list[str] = []
    reasons.extend(group.identifier_conflicts)
    if not identifier_provenance:
        reasons.append("candidate lacks resolved identifier provenance")
    if conflicting_upc:
        reasons.append("barcode mismatch")
    if conflicting_musicbrainz:
        reasons.append("MusicBrainz release ID mismatch")
    if remote_count != remote_song_count:
        if direct_identifier:
            warnings.append(
                "Apple returned an incomplete or unavailable song tracklist; direct "
                "identifier evidence retained the collection"
            )
        else:
            reasons.append("Apple tracklist appears incomplete")
    if local_count != remote_count:
        if direct_identifier:
            warnings.append(
                "track count difference ignored because the candidate is identifier-linked"
            )
        else:
            reasons.append("identifier track count mismatch")
    if direct_identifier and duration_coverage < 0.70:
        warnings.append("duration-fingerprint difference ignored by direct identifier evidence")
    warnings.extend(
        _identifier_presentation_warnings(
            group.logical_tracks,
            candidate.tracks,
            duration_coverage=duration_coverage,
        )
    )

    # A UPC result is a direct Apple identifier link. A release MBID is not exposed by
    # Apple, so MBID-only candidates still need coherent release-level metadata and an
    # order-independent duration fingerprint. Track names and positions never gate this path.
    if (
        not direct_identifier
        and identifier_provenance
        and not conflicting_upc
        and not conflicting_musicbrainz
        and local_count == remote_count
    ):
        if not musicbrainz_search:
            if not artist_compatible:
                reasons.append("identifier candidate artist mismatch")
            if not album_identity_equal and album_score < 0.35:
                reasons.append("identifier candidate album mismatch")
            elif not album_identity_equal:
                warnings.append("album-title difference accepted during identifier reconciliation")
        else:
            if not artist_compatible:
                warnings.append(
                    "local artist-credit difference accepted using resolved MusicBrainz identity"
                )
            if not album_identity_equal:
                warnings.append(
                    "local album-title difference accepted using resolved MusicBrainz identity"
                )
        local_known = sum(track.duration_ms is not None for track in group.logical_tracks)
        remote_known = sum(track.duration_ms is not None for track in candidate.tracks)
        if min(local_known, remote_known) > 0 and duration_coverage < 0.70:
            reasons.append("identifier duration fingerprint mismatch")
    elif direct_identifier:
        if not artist_compatible:
            warnings.append("artist-credit difference ignored by direct identifier evidence")
        if not album_identity_equal:
            warnings.append("album-title difference ignored by direct identifier evidence")

    components = {
        "identifier": 1.0,
        "verified_upc": float(verified_upc),
        "verified_musicbrainz": float(verified_musicbrainz),
        "musicbrainz_release": float(musicbrainz_release),
        "musicbrainz_complete": float(musicbrainz_complete),
        "musicbrainz_search": float(musicbrainz_search),
        "recording_mbid_alignment": float(candidate.musicbrainz_recordings_verified),
        "track_count": count_score,
        "duration_multiset": duration_score,
        "duration_coverage": duration_coverage,
        "album": album_score,
        "artist": artist_score,
        "year": year_score,
        "order_agnostic": 1.0,
        "duration_distance": 1.0 / (1.0 + aggregate_distance / 1_000.0),
    }
    weights = {
        "verified_upc": 0.30,
        "verified_musicbrainz": 0.30,
        "musicbrainz_release": 0.10,
        "musicbrainz_complete": 0.03,
        "recording_mbid_alignment": 0.02,
        "track_count": 0.10,
        "duration_multiset": 0.08,
        "artist": 0.03,
        "album": 0.03,
        "year": 0.01,
    }
    total = sum(components[name] * weight for name, weight in weights.items())
    return CandidateScore(
        candidate=candidate,
        total=total,
        eligible=not reasons,
        reasons=tuple(reasons),
        components=components,
        match_basis=basis,
        warnings=tuple(warnings),
    )


def _score_legacy_candidate(
    group: AlbumGroup,
    candidate: CatalogAlbum,
    *,
    allow_short_releases: bool = False,
) -> CandidateScore:
    """Score a candidate with hard identity and tracklist gates before fuzzy ranking."""
    aligned_release_positions = _aligned_release_label_positions(group, candidate)
    musicbrainz_alignment = _trusted_musicbrainz_alignment(group, candidate)
    album_score = text_similarity(group.album, candidate.album)
    artist_score = text_similarity(group.album_artist, candidate.artist)
    matches = _match_tracks(
        group.logical_tracks,
        candidate.tracks,
        aligned_release_positions=aligned_release_positions,
        trusted_musicbrainz_alignment=musicbrainz_alignment,
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
            float(
                _musicbrainz_track_artists_compatible(group, candidate, local, remote)
                if (local.disc_number or 1, local.track_number) in musicbrainz_alignment
                else _artists_equivalent(local.artist, remote.artist)
            )
            for local, remote, *_ in matches
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
    if aligned_release_positions or musicbrainz_alignment:
        album_score = max(album_score, 0.95)
        year_score = max(year_score, 0.5)
    if musicbrainz_alignment:
        artist_score = max(artist_score, 0.95)

    reasons: list[str] = []
    reasons.extend(group.identifier_conflicts)
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
        and not musicbrainz_alignment
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
    if (
        _has_version_conflict(group.album, candidate.album)
        and not aligned_release_positions
        and not musicbrainz_alignment
    ):
        reasons.append("edition/version conflict")
    if album_score < 0.72:
        reasons.append("album mismatch")
    album_names_related, _album_label_evidence = _release_album_names_related(
        group.album, candidate.album
    )
    if not album_names_related and not musicbrainz_alignment:
        reasons.append("album title mismatch")
    if not _artists_equivalent(group.album_artist, candidate.artist) and not musicbrainz_alignment:
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
    identifier_verified = _barcodes_equivalent(local_barcode, verified_barcode)
    if local_barcode and verified_barcode and not identifier_verified:
        reasons.append("barcode mismatch")
    if (
        len(matches) < 3
        and not identifier_verified
        and not musicbrainz_alignment
        and not allow_short_releases
    ):
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
        "musicbrainz": float(bool(musicbrainz_alignment)),
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
        match_basis="legacy",
    )


def score_candidate(
    group: AlbumGroup,
    candidate: CatalogAlbum,
    *,
    allow_short_releases: bool = False,
) -> CandidateScore:
    """Score through the identifier-first path or the legacy identifier-free fallback."""
    if matching_basis(group) != "legacy":
        return _score_identifier_candidate(group, candidate)
    return _score_legacy_candidate(
        group,
        candidate,
        allow_short_releases=allow_short_releases,
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


def _has_direct_identifier_evidence(score: CandidateScore) -> bool:
    return bool(
        score.components.get("verified_upc") == 1.0
        or (
            score.components.get("verified_musicbrainz") == 1.0
            and score.candidate.identifier_resolution
            in {"musicbrainz_apple_relation", "musicbrainz_barcode"}
        )
    )


def _has_proven_complete_catalog_count(candidate: CatalogAlbum) -> bool:
    if not candidate.tracks or candidate.track_count != len(candidate.tracks):
        return False
    positions = _complete_positions(candidate.tracks)
    if positions is None:
        return False
    by_disc: dict[int, set[int]] = defaultdict(set)
    for disc, number in positions:
        by_disc[disc].add(number)
    return set(by_disc) == set(range(1, max(by_disc, default=0) + 1)) and all(
        numbers == set(range(1, max(numbers) + 1)) for numbers in by_disc.values()
    )


def _unique_direct_identifier_evidence_winner(
    eligible: list[CandidateScore],
) -> tuple[CandidateScore, str] | None:
    """Resolve direct-identifier collisions only with one clearly unique fingerprint.

    Exact identifier lookups can legitimately return multiple Apple collections.  Artwork
    differences remain ambiguous unless local release size or a strong order-independent
    duration fingerprint identifies exactly one of them.  Conflicting count and duration
    winners deliberately leave the result ambiguous.
    """
    if len(eligible) < 2 or not all(_has_direct_identifier_evidence(score) for score in eligible):
        return None

    exact_count = [
        score
        for score in eligible
        if score.components.get("track_count") == 1.0
        and _has_proven_complete_catalog_count(score.candidate)
    ]
    count_winner = exact_count[0] if len(exact_count) == 1 else None

    duration_ranked = sorted(
        eligible,
        key=lambda score: (
            -min(
                score.components.get("duration_coverage", 0.0),
                score.components.get("duration_multiset", 0.0),
            ),
            score.candidate.collection_id,
        ),
    )
    duration_winner: CandidateScore | None = None
    duration_best = duration_ranked[0]
    duration_quality = min(
        duration_best.components.get("duration_coverage", 0.0),
        duration_best.components.get("duration_multiset", 0.0),
    )
    runner_quality = min(
        duration_ranked[1].components.get("duration_coverage", 0.0),
        duration_ranked[1].components.get("duration_multiset", 0.0),
    )
    if duration_quality >= 0.85 and duration_quality - runner_quality >= 0.15:
        duration_winner = duration_best

    evidence = [winner for winner in (count_winner, duration_winner) if winner is not None]
    if not evidence or any(
        winner.candidate.collection_id != evidence[0].candidate.collection_id
        for winner in evidence[1:]
    ):
        return None
    labels = []
    if count_winner is not None:
        labels.append("exact local track count")
    if duration_winner is not None:
        labels.append("duration fingerprint")
    return evidence[0], " and ".join(labels)


def choose_match(
    group: AlbumGroup,
    candidates: Iterable[CatalogAlbum],
    *,
    min_score: float = 0.92,
    min_margin: float = 0.10,
    allow_short_releases: bool = False,
) -> MatchDecision:
    basis = matching_basis(group)
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
            key=lambda score: (-score.total, score.candidate.collection_id),
        )
    )
    eligible = [score for score in scores if score.eligible]
    if not eligible:
        if basis != "legacy":
            return MatchDecision(
                "no_match",
                None,
                scores,
                "no candidate passed identifier-first release gates",
            )
        return MatchDecision(
            "no_match", None, scores, "no candidate passed identity and tracklist gates"
        )
    local_barcode = _normalize_barcode(group.barcode)
    if local_barcode is not None:
        identifier_verified = [
            score
            for score in eligible
            if _barcodes_equivalent(score.candidate.verified_barcode, local_barcode)
        ]
        if identifier_verified:
            eligible = identifier_verified
    best = eligible[0]
    if basis != "legacy":
        tie_break = _unique_direct_identifier_evidence_winner(eligible)
        tie_break_detail: str | None = None
        if tie_break is not None:
            best, tie_break_detail = tie_break
        elif len(eligible) > 1 and not all(
            _equivalent_catalog_releases(best.candidate, runner.candidate)
            and best.candidate.artwork_url == runner.candidate.artwork_url
            for runner in eligible[1:]
        ):
            return MatchDecision(
                "ambiguous",
                None,
                scores,
                "multiple non-equivalent Apple collections share identifier evidence",
            )
        resolution_labels = {
            "embedded_upc": "embedded UPC",
            "musicbrainz_apple_relation": "MusicBrainz Apple relationship",
            "musicbrainz_barcode": "MusicBrainz barcode",
            "musicbrainz_search": "MusicBrainz-resolved search",
        }
        detail = resolution_labels.get(best.candidate.identifier_resolution)
        if detail is None:
            direct_sources = []
            if best.components.get("verified_upc") == 1.0:
                direct_sources.append("UPC")
            if best.components.get("verified_musicbrainz") == 1.0:
                direct_sources.append("MusicBrainz")
            detail = "+".join(direct_sources) if direct_sources else basis
        reason = f"identifier-first match using {detail}"
        if tie_break_detail is not None:
            reason += f"; uniquely supported by {tie_break_detail}"
        return MatchDecision(
            "matched",
            best,
            scores,
            reason,
        )
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
    "matching_basis",
    "normalize_text",
    "score_candidate",
    "text_similarity",
)
