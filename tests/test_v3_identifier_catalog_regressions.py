from __future__ import annotations

import json
from pathlib import Path

import pytest

from apple_artwork import (
    AlbumGroup,
    AppleCatalogClient,
    TrackMetadata,
    candidate_ids_from_album_search,
    catalog_albums_from_lookup,
)
from apple_music_artwork.models import MusicBrainzRelease
from apple_music_artwork.musicbrainz import MusicBrainzClient, _ResolvedMusicBrainzRelease

RELEASE_ID = "12345678-1234-5678-9234-567812345678"
RECORDING_ID = "abcdefab-cdef-4abc-8def-abcdefabcdef"
BARCODE = "012345678905"


class FakeResponse:
    def __init__(self, payload: dict[str, object], *, url: str) -> None:
        self.status_code = 200
        self.url = url
        self.headers = {"Content-Type": "application/json"}
        self._content = json.dumps(payload).encode("utf-8")

    def iter_content(self, chunk_size: int = 64 * 1024):
        for offset in range(0, len(self._content), chunk_size):
            yield self._content[offset : offset + chunk_size]

    def close(self) -> None:
        return None

    def raise_for_status(self) -> None:
        return None


class FakeSession:
    def __init__(self, payloads: list[dict[str, object]]) -> None:
        self.payloads = list(payloads)
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.headers: dict[str, str] = {}

    def get(
        self,
        url: str,
        *,
        params: dict[str, object] | None = None,
        timeout: float,
        allow_redirects: bool = False,
        stream: bool = True,
    ) -> FakeResponse:
        del timeout, allow_redirects, stream
        request_params = params or {}
        self.calls.append((url, request_params))
        if not self.payloads:
            raise AssertionError(f"unexpected Apple request: {url} {request_params}")
        return FakeResponse(self.payloads.pop(0), url=url)


class FakeMusicBrainzClient:
    def __init__(
        self,
        result: MusicBrainzRelease | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.calls: list[str] = []

    def resolve(self, release_id: str) -> MusicBrainzRelease | None:
        self.calls.append(release_id)
        if self.error is not None:
            raise self.error
        return self.result


class FakeValidatedAliasClient:
    def __init__(self, result: _ResolvedMusicBrainzRelease) -> None:
        self.result = result

    def resolve(self, release_id: str) -> _ResolvedMusicBrainzRelease:
        del release_id
        return self.result


class ForgedMusicBrainzSubclass(MusicBrainzClient):
    def __init__(self, result: _ResolvedMusicBrainzRelease) -> None:
        self.result = result

    def resolve(self, release_id: str) -> _ResolvedMusicBrainzRelease:
        del release_id
        return self.result


def _group(
    *,
    barcode: str | None = None,
    release_id: str | None = RELEASE_ID,
    recording_id: str | None = None,
) -> AlbumGroup:
    track = TrackMetadata(
        path=Path("song.flac"),
        title="Local Song Presentation",
        artist="Local Artist Presentation",
        album="Local Album Presentation",
        album_artist="Local Artist Presentation",
        year=2024,
        track_number=1,
        track_total=1,
        disc_number=1,
        disc_total=1,
        duration_ms=100_000,
        barcode=barcode,
        musicbrainz_release_id=release_id,
        musicbrainz_recording_id=recording_id,
    )
    return AlbumGroup(
        album=track.album,
        album_artist=track.album_artist,
        year=track.year,
        files=(track.path,),
        logical_tracks=(track,),
        barcode=barcode,
        musicbrainz_release_id=release_id,
        musicbrainz_provenance_complete=bool(release_id and recording_id),
    )


def _album_rows(
    collection_id: int,
    *,
    album: str = "Apple Album Presentation",
    artist: str = "Apple Artist Presentation",
    count: int = 1,
    year: int = 2024,
) -> list[dict[str, object]]:
    return [
        {
            "wrapperType": "collection",
            "collectionId": collection_id,
            "collectionName": album,
            "artistName": artist,
            "releaseDate": f"{year:04d}-01-01T00:00:00Z",
            "trackCount": count,
            "artworkUrl100": f"https://is1-ssl.mzstatic.com/{collection_id}/100x100bb.jpg",
        },
        *(
            {
                "wrapperType": "track",
                "kind": "song",
                "collectionId": collection_id,
                "collectionName": album,
                "artistName": artist,
                "trackName": f"Apple Track {number}",
                "trackTimeMillis": 100_000 + number,
                "discNumber": 1,
                "trackNumber": number,
            }
            for number in range(1, count + 1)
        ),
    ]


def _client(
    tmp_path: Path,
    *,
    payloads: list[dict[str, object]],
    musicbrainz: FakeMusicBrainzClient,
) -> tuple[AppleCatalogClient, FakeSession]:
    session = FakeSession(payloads)
    return (
        AppleCatalogClient(
            cache_dir=tmp_path / "cache",
            session=session,
            musicbrainz_client=musicbrainz,
            api_interval=0,
        ),
        session,
    )


def test_strict_lookup_accepts_complete_song_rows_when_collection_count_includes_video() -> None:
    rows = _album_rows(99)
    rows[0]["trackCount"] = 2
    rows[1]["trackCount"] = 1
    rows.append(
        {
            "wrapperType": "track",
            "kind": "music-video",
            "collectionId": 99,
            "trackName": "Bonus Video",
        }
    )

    albums = catalog_albums_from_lookup(rows)

    assert len(albums) == 1
    assert albums[0].track_count == 1
    assert len(albums[0].tracks) == 1


def test_strict_lookup_rejects_an_unproven_collection_count_mismatch() -> None:
    rows = _album_rows(99)
    rows[0]["trackCount"] = 2

    assert catalog_albums_from_lookup(rows) == []


def test_embedded_upc_retains_an_artwork_bearing_collection_without_song_rows(
    tmp_path: Path,
) -> None:
    client, _session = _client(
        tmp_path,
        payloads=[{"results": [_album_rows(99)[0]]}],
        musicbrainz=FakeMusicBrainzClient(),
    )

    albums = client.find_candidates(_group(barcode=BARCODE, release_id=None))

    assert [album.collection_id for album in albums] == [99]
    assert albums[0].tracks == ()
    assert albums[0].track_count == 1
    assert albums[0].identifier_resolution == "embedded_upc"
    assert client.last_identifier_warnings == (
        "embedded UPC: Apple collection 99 returned no song rows; direct identifier "
        "evidence retained the artwork-bearing collection",
    )
    diagnostics = client.last_discovery_diagnostics
    json.dumps(diagnostics)
    assert diagnostics["candidate_count"] == 1
    assert diagnostics["stages"]["embedded_upc"] == {
        "raw_rows": 1,
        "collection_rows": 1,
        "song_rows": 0,
        "requested_collections": 1,
        "parsed_collections": 1,
        "rejected_collections": 0,
        "rejection_reasons": {},
        "parser_warnings": [
            "Apple collection 99 returned no song rows; direct identifier evidence retained "
            "the artwork-bearing collection"
        ],
    }


def test_musicbrainz_relation_retains_noncontiguous_provider_topology(
    tmp_path: Path,
) -> None:
    release = MusicBrainzRelease(
        release_id=RELEASE_ID,
        title="Canonical Album",
        artist="Canonical Artist",
        release_year=2024,
        track_count=2,
        barcode=None,
        apple_collection_ids=(99,),
    )
    rows = _album_rows(99, count=2)
    rows[2]["trackNumber"] = 3
    client, _session = _client(
        tmp_path,
        payloads=[{"results": rows}],
        musicbrainz=FakeMusicBrainzClient(release),
    )

    albums = client.find_candidates(_group())

    assert [album.collection_id for album in albums] == [99]
    assert [track.track_number for track in albums[0].tracks] == [1, 3]
    assert albums[0].identifier_resolution == "musicbrainz_apple_relation"
    assert client.last_identifier_warnings == (
        "MusicBrainz Apple relationship: Apple collection 99 returned non-contiguous "
        "disc/track topology; direct identifier evidence retained the provider presentation",
    )


def test_musicbrainz_barcode_retains_collection_only_identifier_result(
    tmp_path: Path,
) -> None:
    release = MusicBrainzRelease(
        release_id=RELEASE_ID,
        title="Canonical Album",
        artist="Canonical Artist",
        release_year=2024,
        track_count=1,
        barcode=BARCODE,
    )
    client, _session = _client(
        tmp_path,
        payloads=[{"results": [_album_rows(99)[0]]}],
        musicbrainz=FakeMusicBrainzClient(release),
    )

    albums = client.find_candidates(_group())

    assert [album.collection_id for album in albums] == [99]
    assert albums[0].tracks == ()
    assert albums[0].verified_barcode == BARCODE
    assert albums[0].identifier_resolution == "musicbrainz_barcode"
    assert client.last_identifier_warnings == (
        "MusicBrainz barcode: Apple collection 99 returned no song rows; direct identifier "
        "evidence retained the artwork-bearing collection",
    )


def test_identifier_album_search_ranking_is_independent_of_provider_row_order() -> None:
    rows = [
        {
            "collectionId": collection_id,
            "collectionName": "Canonical Album",
            "artistName": "Canonical Artist",
            "releaseDate": "2024-01-01T00:00:00Z",
            "trackCount": 1,
            "artworkUrl100": f"https://example.invalid/{collection_id}.jpg",
        }
        for collection_id in (30, 10, 20)
    ]

    forward = candidate_ids_from_album_search(
        rows,
        "Canonical Artist",
        "Canonical Album",
        track_count=1,
        release_year=2024,
        identifier_first=True,
    )
    reverse = candidate_ids_from_album_search(
        reversed(rows),
        "Canonical Artist",
        "Canonical Album",
        track_count=1,
        release_year=2024,
        identifier_first=True,
    )

    assert forward == reverse == [10, 20, 30]


def test_identifier_album_search_rejects_a_different_feature_credit() -> None:
    rows = [
        {
            "collectionId": 99,
            "collectionName": "Signal (feat. Bob)",
            "artistName": "Primary",
            "releaseDate": "2024-01-01T00:00:00Z",
            "trackCount": 1,
            "artworkUrl100": "https://example.invalid/99.jpg",
        }
    ]

    assert (
        candidate_ids_from_album_search(
            rows,
            "Primary",
            "Signal (feat. Alice)",
            track_count=1,
            release_year=2024,
            identifier_first=True,
        )
        == []
    )


@pytest.mark.parametrize(
    ("resolved_album", "resolved_artist", "candidate_artist"),
    (
        ("Signal (feat. Alice)", "Primary", "Primary & Alice"),
        ("Signal (feat. Alice & Bob)", "Primary", "Primary, Alice & Bob"),
        ("Signal (feat. Alice)", "The Primary", "Primary & Alice"),
    ),
)
def test_identifier_album_search_accepts_known_features_in_unmarked_artist_suffix(
    resolved_album: str,
    resolved_artist: str,
    candidate_artist: str,
) -> None:
    rows = [
        {
            "collectionId": 99,
            "collectionName": "Signal",
            "artistName": candidate_artist,
            "releaseDate": "2024-01-01T00:00:00Z",
            "trackCount": 1,
            "artworkUrl100": "https://example.invalid/99.jpg",
        }
    ]

    assert candidate_ids_from_album_search(
        rows,
        resolved_artist,
        resolved_album,
        track_count=1,
        release_year=2024,
        identifier_first=True,
    ) == [99]


@pytest.mark.parametrize(
    ("resolved_album", "resolved_artist", "candidate_artist"),
    (
        ("Signal (feat. Supply)", "Air", "Air Supply"),
        ("Signal (feat. Smith)", "John", "John Smith"),
    ),
)
def test_identifier_album_search_rejects_unmarked_artist_without_a_delimiter(
    resolved_album: str,
    resolved_artist: str,
    candidate_artist: str,
) -> None:
    rows = [
        {
            "collectionId": 99,
            "collectionName": "Signal",
            "artistName": candidate_artist,
            "releaseDate": "2024-01-01T00:00:00Z",
            "trackCount": 1,
            "artworkUrl100": "https://example.invalid/99.jpg",
        }
    ]

    assert (
        candidate_ids_from_album_search(
            rows,
            resolved_artist,
            resolved_album,
            track_count=1,
            release_year=2024,
            identifier_first=True,
        )
        == []
    )


def test_identifier_album_search_does_not_treat_lexical_with_as_an_unmarked_delimiter() -> None:
    rows = [
        {
            "collectionId": 99,
            "collectionName": "Signal",
            "artistName": "Sleeping With Sirens",
            "releaseDate": "2024-01-01T00:00:00Z",
            "trackCount": 1,
            "artworkUrl100": "https://example.invalid/99.jpg",
        }
    ]

    assert (
        candidate_ids_from_album_search(
            rows,
            "Sleeping",
            "Signal (feat. Sirens)",
            track_count=1,
            release_year=2024,
            identifier_first=True,
        )
        == []
    )


def test_identifier_album_search_does_not_split_lexical_with() -> None:
    rows = [
        {
            "collectionId": 99,
            "collectionName": "Sequence",
            "artistName": "Artist feat. Tokens",
            "releaseDate": "2024-01-01T00:00:00Z",
            "trackCount": 1,
            "artworkUrl100": "https://example.invalid/99.jpg",
        }
    ]

    assert (
        candidate_ids_from_album_search(
            rows,
            "Artist",
            "Sequence with Tokens",
            track_count=1,
            release_year=2024,
            identifier_first=True,
        )
        == []
    )


def test_musicbrainz_relation_lookup_ignores_unrequested_collection_rows(
    tmp_path: Path,
) -> None:
    group = _group()
    release = MusicBrainzRelease(
        release_id=RELEASE_ID,
        title="Canonical Album",
        artist="Canonical Artist",
        release_year=2024,
        track_count=1,
        barcode=None,
        apple_collection_ids=(99,),
    )
    client, session = _client(
        tmp_path,
        payloads=[{"results": [*_album_rows(777), *_album_rows(99)]}],
        musicbrainz=FakeMusicBrainzClient(release),
    )

    albums = client.find_candidates(group)

    assert [album.collection_id for album in albums] == [99]
    assert session.calls[0][1]["id"] == "99"


def test_catalog_rejects_a_resolver_returning_a_different_release_id(
    tmp_path: Path,
) -> None:
    different_release_id = "87654321-4321-8765-9321-876543218765"
    release = MusicBrainzRelease(
        release_id=different_release_id,
        title="Wrong Album",
        artist="Wrong Artist",
        release_year=2024,
        track_count=1,
        barcode=None,
        apple_collection_ids=(99,),
    )
    client, session = _client(
        tmp_path,
        payloads=[],
        musicbrainz=FakeMusicBrainzClient(release),
    )

    assert client.find_candidates(_group()) == []
    assert session.calls == []
    assert client.last_identifier_warnings == (
        "MusicBrainz returned a different release MBID than the embedded identifier",
    )


def test_catalog_rejects_a_custom_resolver_self_asserting_a_merged_alias(
    tmp_path: Path,
) -> None:
    canonical_release_id = "87654321-4321-8765-9321-876543218765"
    release = MusicBrainzRelease(
        release_id=canonical_release_id,
        title="Canonical Album",
        artist="Canonical Artist",
        release_year=2024,
        track_count=1,
        barcode=None,
        apple_collection_ids=(99,),
    )
    client, _session = _client(
        tmp_path,
        payloads=[],
        musicbrainz=FakeMusicBrainzClient(release),
    )

    assert client.find_candidates(_group()) == []
    assert client.last_identifier_warnings == (
        "MusicBrainz returned a different release MBID than the embedded identifier",
    )


def test_catalog_rejects_custom_client_forging_merged_release_alias(tmp_path: Path) -> None:
    resolution = _ResolvedMusicBrainzRelease(
        release=MusicBrainzRelease(
            release_id="87654321-4321-8765-9321-876543218765",
            title="Canonical Album",
            artist="Canonical Artist",
            release_year=2024,
            track_count=1,
            barcode=None,
            apple_collection_ids=(99,),
        ),
        requested_release_id=RELEASE_ID,
    )
    session = FakeSession([])
    client = AppleCatalogClient(
        cache_dir=tmp_path / "cache",
        session=session,
        musicbrainz_client=FakeValidatedAliasClient(resolution),
        api_interval=0,
    )

    assert client.find_candidates(_group()) == []
    assert session.calls == []
    assert client.last_identifier_warnings == (
        "a custom MusicBrainz resolver cannot assert merged-release alias provenance",
    )


def test_catalog_rejects_subclass_forging_merged_release_alias(tmp_path: Path) -> None:
    resolution = _ResolvedMusicBrainzRelease(
        release=MusicBrainzRelease(
            release_id="87654321-4321-8765-9321-876543218765",
            title="Wrong Album",
            artist="Wrong Artist",
            release_year=2024,
            track_count=1,
            barcode=None,
            apple_collection_ids=(99,),
        ),
        requested_release_id=RELEASE_ID,
    )
    session = FakeSession([])
    client = AppleCatalogClient(
        cache_dir=tmp_path / "cache",
        session=session,
        musicbrainz_client=ForgedMusicBrainzSubclass(resolution),
        api_interval=0,
    )

    assert client.find_candidates(_group()) == []
    assert session.calls == []
    assert client.last_identifier_warnings == (
        "a custom MusicBrainz resolver cannot assert merged-release alias provenance",
    )


def test_catalog_normalizes_recording_ids_returned_by_custom_resolver(
    tmp_path: Path,
) -> None:
    release = MusicBrainzRelease(
        release_id=RELEASE_ID.upper(),
        title="Canonical Album",
        artist="Canonical Artist",
        release_year=2024,
        track_count=1,
        barcode=None,
        apple_collection_ids=(99,),
        recording_ids=(RECORDING_ID.upper(),),
    )
    client, _session = _client(
        tmp_path,
        payloads=[{"results": _album_rows(99)}],
        musicbrainz=FakeMusicBrainzClient(release),
    )

    albums = client.find_candidates(_group(recording_id=RECORDING_ID))

    assert [album.collection_id for album in albums] == [99]
    assert albums[0].musicbrainz_recordings_verified is True


@pytest.mark.parametrize(
    "changes",
    (
        {"recording_ids": None},
        {"recording_ids": (RECORDING_ID, "not-a-uuid")},
        {"apple_collection_ids": None},
        {"apple_collection_ids": (99, "garbage")},
        {"barcode": 123},
        {"barcode": "not-a-barcode"},
        {"release_year": True},
        {"track_count": -1},
        {"title": None},
        {"artist": None},
    ),
)
def test_catalog_fails_closed_on_malformed_custom_resolution(
    tmp_path: Path,
    changes: dict[str, object],
) -> None:
    release = MusicBrainzRelease(
        release_id=RELEASE_ID,
        title="Canonical Album",
        artist="Canonical Artist",
        release_year=2024,
        track_count=1,
        barcode=None,
        apple_collection_ids=(99,),
        recording_ids=(RECORDING_ID,),
    )
    malformed = object.__new__(MusicBrainzRelease)
    for field in (
        "release_id",
        "title",
        "artist",
        "release_year",
        "track_count",
        "barcode",
        "apple_collection_ids",
        "recording_ids",
    ):
        object.__setattr__(malformed, field, changes.get(field, getattr(release, field)))
    client, session = _client(
        tmp_path,
        payloads=[],
        musicbrainz=FakeMusicBrainzClient(malformed),
    )

    assert client.find_candidates(_group()) == []
    assert session.calls == []
    assert client.last_identifier_warnings == (
        "MusicBrainz returned malformed release resolution evidence",
    )


def test_dual_identifiers_cross_validate_a_consistent_exact_upc(
    tmp_path: Path,
) -> None:
    group = _group(barcode=BARCODE, recording_id=RECORDING_ID)
    release = MusicBrainzRelease(
        release_id=RELEASE_ID,
        title="Canonical Album",
        artist="Canonical Artist",
        release_year=2024,
        track_count=1,
        barcode=BARCODE,
        apple_collection_ids=(99,),
        recording_ids=(RECORDING_ID,),
    )
    musicbrainz = FakeMusicBrainzClient(release)
    client, session = _client(
        tmp_path,
        payloads=[{"results": _album_rows(99)}],
        musicbrainz=musicbrainz,
    )

    albums = client.find_candidates(group)

    assert [album.collection_id for album in albums] == [99]
    assert albums[0].verified_barcode == BARCODE
    assert albums[0].verified_musicbrainz_release_id == RELEASE_ID
    assert albums[0].identifier_resolution == "embedded_upc"
    assert albums[0].musicbrainz_recordings_verified is True
    assert musicbrainz.calls == [RELEASE_ID]
    assert len(session.calls) == 1
    assert client.last_identifier_warnings == ()


@pytest.mark.parametrize(
    ("musicbrainz", "warning"),
    (
        (
            FakeMusicBrainzClient(None),
            "MusicBrainz did not resolve the embedded release MBID; the exact UPC match "
            "was retained but the MBID could not be cross-validated",
        ),
        (
            FakeMusicBrainzClient(error=RuntimeError("offline")),
            "the MusicBrainz release lookup failed; the exact UPC match was retained but "
            "the release MBID could not be cross-validated",
        ),
    ),
)
def test_dual_identifiers_retain_exact_upc_when_mbid_cannot_be_checked(
    tmp_path: Path,
    musicbrainz: FakeMusicBrainzClient,
    warning: str,
) -> None:
    client, _session = _client(
        tmp_path,
        payloads=[{"results": _album_rows(99)}],
        musicbrainz=musicbrainz,
    )

    albums = client.find_candidates(_group(barcode=BARCODE))

    assert [album.collection_id for album in albums] == [99]
    assert albums[0].verified_musicbrainz_release_id is None
    assert client.last_identifier_warnings == (warning,)


def test_dual_identifiers_block_a_resolved_musicbrainz_barcode_conflict(
    tmp_path: Path,
) -> None:
    release = MusicBrainzRelease(
        release_id=RELEASE_ID,
        title="Canonical Album",
        artist="Canonical Artist",
        release_year=2024,
        track_count=1,
        barcode="4006381333931",
    )
    client, session = _client(
        tmp_path,
        payloads=[{"results": _album_rows(99)}],
        musicbrainz=FakeMusicBrainzClient(release),
    )

    assert client.find_candidates(_group(barcode=BARCODE)) == []
    assert len(session.calls) == 1
    assert client.last_identifier_warnings == (
        "the embedded UPC conflicts with the resolved MusicBrainz release barcode",
    )


def test_dual_identifiers_block_disjoint_apple_collection_crosswalks(
    tmp_path: Path,
) -> None:
    release = MusicBrainzRelease(
        release_id=RELEASE_ID,
        title="Canonical Album",
        artist="Canonical Artist",
        release_year=2024,
        track_count=1,
        barcode=None,
        apple_collection_ids=(100,),
    )
    client, _session = _client(
        tmp_path,
        payloads=[{"results": _album_rows(99)}],
        musicbrainz=FakeMusicBrainzClient(release),
    )

    assert client.find_candidates(_group(barcode=BARCODE)) == []
    assert client.last_identifier_warnings == (
        "the embedded UPC and MusicBrainz release point to different Apple collections",
    )


def test_dual_identifiers_keep_barcode_consistent_storefront_upc_result(
    tmp_path: Path,
) -> None:
    release = MusicBrainzRelease(
        release_id=RELEASE_ID,
        title="Canonical Album",
        artist="Canonical Artist",
        release_year=2024,
        track_count=1,
        barcode=BARCODE,
        apple_collection_ids=(100,),
    )
    client, _session = _client(
        tmp_path,
        payloads=[{"results": _album_rows(99)}],
        musicbrainz=FakeMusicBrainzClient(release),
    )

    albums = client.find_candidates(_group(barcode=BARCODE))

    assert [album.collection_id for album in albums] == [99]
    assert albums[0].verified_musicbrainz_release_id == RELEASE_ID
    assert client.last_identifier_warnings == (
        "the MusicBrainz Apple relationship points to a different storefront collection; "
        "the barcode-consistent exact UPC result was retained",
    )


def test_dual_identifiers_retain_upc_when_resolved_mbid_has_no_crosswalk(
    tmp_path: Path,
) -> None:
    release = MusicBrainzRelease(
        release_id=RELEASE_ID,
        title="Canonical Album",
        artist="Canonical Artist",
        release_year=2024,
        track_count=1,
        barcode=None,
    )
    client, _session = _client(
        tmp_path,
        payloads=[{"results": _album_rows(99)}],
        musicbrainz=FakeMusicBrainzClient(release),
    )

    albums = client.find_candidates(_group(barcode=BARCODE))

    assert [album.collection_id for album in albums] == [99]
    assert albums[0].verified_musicbrainz_release_id is None
    assert client.last_identifier_warnings == (
        "the MusicBrainz release supplied no direct Apple or barcode crosswalk; the exact "
        "UPC match was retained",
    )


def test_failed_embedded_upc_can_continue_through_a_consistent_mbid_relation(
    tmp_path: Path,
) -> None:
    release = MusicBrainzRelease(
        release_id=RELEASE_ID,
        title="Canonical Album",
        artist="Canonical Artist",
        release_year=2024,
        track_count=1,
        barcode=BARCODE,
        apple_collection_ids=(99,),
    )
    client, session = _client(
        tmp_path,
        payloads=[{"results": []}, {"results": _album_rows(99)}],
        musicbrainz=FakeMusicBrainzClient(release),
    )

    albums = client.find_candidates(_group(barcode=BARCODE))

    assert [album.collection_id for album in albums] == [99]
    assert albums[0].identifier_resolution == "musicbrainz_apple_relation"
    assert len(session.calls) == 2
    assert session.calls[0][1]["upc"] == BARCODE
    assert session.calls[1][1]["id"] == "99"
    assert client.last_identifier_warnings == (
        "the embedded UPC returned no usable artwork-bearing Apple collection",
    )


def test_mbid_search_defers_count_and_year_gates_until_after_lookup(
    tmp_path: Path,
) -> None:
    group = _group()
    release = MusicBrainzRelease(
        release_id=RELEASE_ID,
        title="Canonical Album (2011 Remaster)",
        artist="Canonical Artist",
        release_year=2024,
        track_count=1,
        barcode=None,
    )
    search_rows = [
        {
            "collectionId": 99,
            "collectionName": "Canonical Album (2011 Remaster)",
            "artistName": "Canonical Artist",
            "releaseDate": "2024-01-01T00:00:00Z",
            "trackCount": 1,
            "artworkUrl100": "https://example.invalid/99.jpg",
        },
        {
            "collectionId": 100,
            "collectionName": "Canonical Album (2011 Remaster)",
            "artistName": "Canonical Artist",
            "releaseDate": "2024-01-01T00:00:00Z",
            "trackCount": 2,
            "artworkUrl100": "https://example.invalid/100.jpg",
        },
        {
            "collectionId": 101,
            "collectionName": "Canonical Album (2015 Remaster)",
            "artistName": "Canonical Artist",
            "releaseDate": "2024-01-01T00:00:00Z",
            "trackCount": 1,
            "artworkUrl100": "https://example.invalid/101.jpg",
        },
        {
            "collectionId": 102,
            "collectionName": "Canonical Album (2011 Remaster)",
            "artistName": "Canonical Artist",
            "releaseDate": "2028-01-01T00:00:00Z",
            "trackCount": 1,
            "artworkUrl100": "https://example.invalid/102.jpg",
        },
    ]
    client, session = _client(
        tmp_path,
        payloads=[
            {"results": search_rows},
            {
                "results": [
                    *_album_rows(
                        99,
                        album="Canonical Album (2011 Remaster)",
                        artist="Canonical Artist",
                    ),
                    *_album_rows(
                        102,
                        album="Canonical Album (2011 Remaster)",
                        artist="Canonical Artist",
                        year=2028,
                    ),
                    *_album_rows(
                        100,
                        album="Canonical Album (2011 Remaster)",
                        artist="Canonical Artist",
                        count=2,
                    ),
                ]
            },
        ],
        musicbrainz=FakeMusicBrainzClient(release),
    )

    albums = client.find_candidates(group)

    assert [album.collection_id for album in albums] == [99]
    assert session.calls[1][1]["id"] == "99,102,100"
    album = albums[0]
    assert album.identifier_resolution == "musicbrainz_search"
    diagnostics = client.last_discovery_diagnostics
    json.dumps(diagnostics)
    assert diagnostics["resolved_musicbrainz"] == {
        "status": "resolved",
        "requested_release_id": RELEASE_ID,
        "resolved_release_id": RELEASE_ID,
        "canonical_alias": False,
        "title": "Canonical Album (2011 Remaster)",
        "artist": "Canonical Artist",
        "track_count": 1,
        "release_year": 2024,
        "barcode": None,
        "apple_collection_ids": [],
        "recording_id_count": 0,
    }
    search_diagnostics = diagnostics["stages"]["album_search"]
    assert search_diagnostics["raw_rows"] == 4
    assert search_diagnostics["selected_collections"] == 3
    assert search_diagnostics["rejected_collections"] == 1
    assert search_diagnostics["rejection_reasons"] == {"explicit remaster-year conflict": 1}
    assert search_diagnostics["lookup"]["parsed_collections"] == 3
    assert search_diagnostics["postlookup_accepted_collections"] == 1
    assert search_diagnostics["postlookup_rejected_collections"] == 2
    assert search_diagnostics["postlookup_rejection_reasons"] == {
        "resolved MusicBrainz release-year mismatch": 1,
        "resolved MusicBrainz track-count mismatch": 1,
    }
    assert album.verified_musicbrainz_release_id == RELEASE_ID
    assert album.resolved_musicbrainz_title == release.title
    assert album.resolved_musicbrainz_artist == release.artist
    assert album.resolved_musicbrainz_track_count == release.track_count
    assert album.resolved_musicbrainz_release_year == release.release_year


def test_mbid_search_uses_local_count_when_resolved_release_omits_count(
    tmp_path: Path,
) -> None:
    release = MusicBrainzRelease(
        release_id=RELEASE_ID,
        title="Canonical Album",
        artist="Canonical Artist",
        release_year=2024,
        track_count=None,
        barcode=None,
    )
    search_rows = [
        {
            "collectionId": 99,
            "collectionName": "Canonical Album",
            "artistName": "Canonical Artist",
            "releaseDate": "2024-01-01T00:00:00Z",
            "trackCount": 1,
            "artworkUrl100": "https://example.invalid/99.jpg",
        }
    ]
    client, _session = _client(
        tmp_path,
        payloads=[
            {"results": search_rows},
            {
                "results": _album_rows(
                    99,
                    album="Canonical Album",
                    artist="Canonical Artist",
                )
            },
        ],
        musicbrainz=FakeMusicBrainzClient(release),
    )

    albums = client.find_candidates(_group())

    assert [album.collection_id for album in albums] == [99]
    assert albums[0].resolved_musicbrainz_track_count is None
    assert albums[0].musicbrainz_search_track_count == 1
    assert albums[0].musicbrainz_search_track_count_source == "local"
    assert albums[0].identifier_resolution == "musicbrainz_search"


@pytest.mark.parametrize(
    "lookup_rows",
    (
        _album_rows(
            99,
            album="Canonical Album (2015 Remaster)",
            artist="Canonical Artist",
        ),
        _album_rows(
            99,
            album="Canonical Album (2011 Remaster)",
            artist="Canonical Artist",
            count=2,
        ),
    ),
)
def test_mbid_search_rechecks_canonical_remaster_and_count_after_lookup(
    tmp_path: Path,
    lookup_rows: list[dict[str, object]],
) -> None:
    release = MusicBrainzRelease(
        release_id=RELEASE_ID,
        title="Canonical Album (2011 Remaster)",
        artist="Canonical Artist",
        release_year=2024,
        track_count=1,
        barcode=None,
    )
    search_rows = [
        {
            "collectionId": 99,
            "collectionName": release.title,
            "artistName": release.artist,
            "releaseDate": "2024-01-01T00:00:00Z",
            "trackCount": 1,
            "artworkUrl100": "https://example.invalid/99.jpg",
        }
    ]
    client, _session = _client(
        tmp_path,
        payloads=[{"results": search_rows}, {"results": lookup_rows}],
        musicbrainz=FakeMusicBrainzClient(release),
    )

    assert client.find_candidates(_group()) == []
    assert client.last_identifier_warnings == (
        "the identifier-authoritative Apple search returned no usable complete album",
    )
