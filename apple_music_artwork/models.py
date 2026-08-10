"""Shared immutable data models and domain errors."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class TrackMetadata:
    """Catalog-matching fields plus the filesystem identity observed during the scan."""

    path: Path
    title: str
    artist: str
    album: str
    album_artist: str
    year: int | None = None
    track_number: int | None = None
    track_total: int | None = None
    disc_number: int | None = None
    disc_total: int | None = None
    duration_ms: int | None = None
    barcode: str | None = None
    musicbrainz_release_id: str | None = None
    source_identity: tuple[int, int, int, int, int] | None = None


@dataclass(frozen=True, slots=True)
class AlbumGroup:
    album: str
    album_artist: str
    year: int | None
    files: tuple[Path, ...]
    logical_tracks: tuple[TrackMetadata, ...]
    barcode: str | None = None
    musicbrainz_release_id: str | None = None
    source_identities: tuple[tuple[Path, tuple[int, int, int, int, int]], ...] = ()


@dataclass(frozen=True, slots=True)
class CatalogTrack:
    title: str
    artist: str
    duration_ms: int | None
    disc_number: int | None
    track_number: int | None


@dataclass(frozen=True, slots=True)
class CatalogAlbum:
    collection_id: int
    album: str
    artist: str
    release_year: int | None
    artwork_url: str
    track_count: int | None
    tracks: tuple[CatalogTrack, ...]
    verified_barcode: str | None = None


@dataclass(frozen=True, slots=True)
class CandidateScore:
    candidate: CatalogAlbum
    total: float
    eligible: bool
    reasons: tuple[str, ...]
    components: Mapping[str, float]


@dataclass(frozen=True, slots=True)
class MatchDecision:
    status: str
    match: CandidateScore | None
    scores: tuple[CandidateScore, ...]
    reason: str


@dataclass(frozen=True, slots=True)
class Artwork:
    data: bytes
    mime: str
    width: int
    height: int
    depth: int
    source_url: str
    sha256: str


class ArtworkError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class EmbedResult:
    status: str
    format: str
    message: str


class EmbedError(RuntimeError):
    pass


class EmbedCommittedInterrupt(KeyboardInterrupt):
    """An interrupt arrived after the replacement became irreversible."""

    committed = True

    def __init__(self, message: str, result: EmbedResult, path: Path) -> None:
        super().__init__(message)
        self.result = result
        self.path = path
        self.report_persisted = False


class EmbedCommittedError(EmbedError):
    """The replacement occurred, but post-commit durability/verification was uncertain."""

    committed = True

    def __init__(self, message: str, result: EmbedResult) -> None:
        super().__init__(message)
        self.result = result


__all__ = (
    "AlbumGroup",
    "Artwork",
    "ArtworkError",
    "CandidateScore",
    "CatalogAlbum",
    "CatalogTrack",
    "EmbedCommittedError",
    "EmbedCommittedInterrupt",
    "EmbedError",
    "EmbedResult",
    "MatchDecision",
    "TrackMetadata",
)
