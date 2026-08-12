"""Identifier-first Apple Music artwork matching and embedding."""

from .artwork import ArtworkDownloader, build_artwork_urls, decode_artwork
from .catalog import (
    AppleCatalogClient,
    candidate_ids_from_album_search,
    candidate_ids_from_song_search,
    catalog_albums_from_lookup,
)
from .cli import main
from .constants import VERSION
from .embedding import embed_artwork, preflight_artwork
from .matching import choose_match, matching_basis, normalize_text, score_candidate, text_similarity
from .metadata import discover_audio_files, group_tracks, read_track_metadata
from .models import (
    AlbumGroup,
    Artwork,
    ArtworkError,
    CandidateScore,
    CatalogAlbum,
    CatalogTrack,
    EmbedCommittedError,
    EmbedCommittedInterrupt,
    EmbedError,
    EmbedResult,
    MatchDecision,
    MusicBrainzRelease,
    TrackMetadata,
)
from .musicbrainz import MusicBrainzClient, parse_musicbrainz_release
from .pipeline import process_library

__all__ = (
    "VERSION",
    "AlbumGroup",
    "AppleCatalogClient",
    "Artwork",
    "ArtworkDownloader",
    "ArtworkError",
    "CandidateScore",
    "CatalogAlbum",
    "CatalogTrack",
    "EmbedCommittedError",
    "EmbedCommittedInterrupt",
    "EmbedError",
    "EmbedResult",
    "MatchDecision",
    "MusicBrainzClient",
    "MusicBrainzRelease",
    "TrackMetadata",
    "build_artwork_urls",
    "candidate_ids_from_album_search",
    "candidate_ids_from_song_search",
    "catalog_albums_from_lookup",
    "choose_match",
    "decode_artwork",
    "discover_audio_files",
    "embed_artwork",
    "group_tracks",
    "main",
    "matching_basis",
    "normalize_text",
    "parse_musicbrainz_release",
    "preflight_artwork",
    "process_library",
    "read_track_metadata",
    "score_candidate",
    "text_similarity",
)
