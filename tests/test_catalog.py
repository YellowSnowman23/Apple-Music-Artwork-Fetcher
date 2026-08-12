import json
from dataclasses import replace
from pathlib import Path

from apple_artwork import (
    AlbumGroup,
    AppleCatalogClient,
    TrackMetadata,
    candidate_ids_from_album_search,
    candidate_ids_from_song_search,
    catalog_albums_from_lookup,
    choose_match,
)
from apple_music_artwork.models import MusicBrainzRelease


class FakeMusicBrainzClient:
    def __init__(self, release: MusicBrainzRelease | None) -> None:
        self.release = release
        self.calls: list[str] = []

    def resolve(self, release_id: str) -> MusicBrainzRelease | None:
        self.calls.append(release_id)
        return self.release


def test_catalog_albums_from_lookup_groups_collection_and_song_rows() -> None:
    rows = [
        {
            "wrapperType": "collection",
            "collectionType": "Album",
            "collectionId": 42,
            "collectionName": "In Rainbows",
            "artistName": "Radiohead",
            "releaseDate": "2007-12-28T08:00:00Z",
            "trackCount": 2,
            "artworkUrl100": "https://is1-ssl.mzstatic.com/a/100x100bb.jpg",
        },
        {
            "wrapperType": "track",
            "kind": "song",
            "collectionId": 42,
            "trackName": "15 Step",
            "artistName": "Radiohead",
            "trackTimeMillis": 237_293,
            "discNumber": 1,
            "trackNumber": 1,
        },
        {
            "wrapperType": "track",
            "kind": "song",
            "collectionId": 42,
            "trackName": "Bodysnatchers",
            "artistName": "Radiohead",
            "trackTimeMillis": 242_293,
            "discNumber": 1,
            "trackNumber": 2,
        },
    ]

    albums = catalog_albums_from_lookup(rows)

    assert len(albums) == 1
    album = albums[0]
    assert album.collection_id == 42
    assert album.release_year == 2007
    assert [track.title for track in album.tracks] == ["15 Step", "Bodysnatchers"]


def test_album_search_candidates_apply_an_artist_gate_before_lookup() -> None:
    rows = [
        {
            "collectionId": 1,
            "collectionName": "Greatest Hits",
            "artistName": "Wrong Artist",
            "artworkUrl100": "https://example.invalid/1.jpg",
        },
        {
            "collectionId": 2,
            "collectionName": "Greatest Hits",
            "artistName": "Alpha",
            "artworkUrl100": "https://example.invalid/2.jpg",
        },
        {
            "collectionId": 3,
            "collectionName": "Unrelated Album",
            "artistName": "Alpha",
            "artworkUrl100": "https://example.invalid/3.jpg",
        },
    ]

    assert candidate_ids_from_album_search(rows, "Alpha", "Greatest Hits") == [2]


def test_album_search_keeps_exact_artist_equal_count_edition_candidate() -> None:
    rows = [
        {
            "collectionId": 1873373091,
            "collectionName": "5150 (Expanded Edition)",
            "artistName": "Van Halen",
            "trackCount": 30,
            "artworkUrl100": "https://example.invalid/5150.jpg",
        }
    ]

    assert candidate_ids_from_album_search(
        rows,
        "Van Halen",
        "5150",
        track_count=30,
    ) == [1873373091]


def test_identifier_album_search_never_uses_track_count_as_identity() -> None:
    rows = [
        {
            "collectionId": 1,
            "collectionName": "Completely Unrelated",
            "artistName": "Wrong Artist",
            "trackCount": 6,
            "artworkUrl100": "https://example.invalid/wrong.jpg",
        }
    ]

    assert (
        candidate_ids_from_album_search(
            rows,
            "Canonical Artist",
            "Canonical Album",
            track_count=6,
            identifier_first=True,
        )
        == []
    )


def test_song_search_accepts_provider_omitted_local_remaster_for_discovery() -> None:
    rows = [
        {
            "wrapperType": "track",
            "kind": "song",
            "collectionId": 976835132,
            "collectionName": "Fair Warning",
            "trackName": "Mean Street",
            "artistName": "Van Halen",
        }
    ]

    assert candidate_ids_from_song_search(
        rows,
        artist="Van Halen",
        album="Fair Warning",
        title="Mean Street (2015 Remaster)",
    ) == [976835132]


def test_song_search_can_discover_provider_explicit_remaster_for_full_validation() -> None:
    rows = [
        {
            "wrapperType": "track",
            "kind": "song",
            "collectionId": 976835132,
            "collectionName": "Fair Warning",
            "trackName": "Mean Street (2015 Remaster)",
            "artistName": "Van Halen",
        }
    ]

    assert candidate_ids_from_song_search(
        rows,
        artist="Van Halen",
        album="Fair Warning",
        title="Mean Street",
    ) == [976835132]


def test_song_search_reconciles_album_version_and_remaster_for_discovery() -> None:
    rows = [
        {
            "wrapperType": "track",
            "kind": "song",
            "collectionId": 1890580956,
            "collectionName": "Weezer (2024 Remaster)",
            "trackName": "My Name Is Jonas (2024 Remaster)",
            "artistName": "Weezer",
        }
    ]

    assert candidate_ids_from_song_search(
        rows,
        artist="Weezer",
        album="Weezer (The Blue Album)",
        title="My Name Is Jonas (Album Version)",
    ) == [1890580956]


class FakeResponse:
    def __init__(
        self, payload: dict[str, object], status_code: int = 200, *, url: str = ""
    ) -> None:
        self.payload = payload
        self.status_code = status_code
        self.url = url
        self.headers: dict[str, str] = {"Content-Type": "application/json"}
        self._content = json.dumps(payload).encode("utf-8")

    def iter_content(self, chunk_size: int = 64 * 1024):
        for offset in range(0, len(self._content), chunk_size):
            yield self._content[offset : offset + chunk_size]

    def close(self) -> None:
        return None

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


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
        return FakeResponse(self.payloads.pop(0), url=url)


def test_catalog_client_searches_then_expands_tracks_and_reuses_disk_cache(tmp_path: Path) -> None:
    search_rows = [
        {
            "collectionId": 42,
            "collectionName": "In Rainbows",
            "artistName": "Radiohead",
            "artworkUrl100": "https://is1-ssl.mzstatic.com/a/100x100bb.jpg",
        }
    ]
    lookup_rows = [
        {
            "wrapperType": "collection",
            "collectionType": "Album",
            "collectionId": 42,
            "collectionName": "In Rainbows",
            "artistName": "Radiohead",
            "releaseDate": "2007-12-28T08:00:00Z",
            "trackCount": 1,
            "artworkUrl100": "https://is1-ssl.mzstatic.com/a/100x100bb.jpg",
        },
        {
            "wrapperType": "track",
            "kind": "song",
            "collectionId": 42,
            "trackName": "15 Step",
            "artistName": "Radiohead",
            "trackTimeMillis": 237_293,
            "discNumber": 1,
            "trackNumber": 1,
        },
    ]
    session = FakeSession([{"results": search_rows}, {"results": lookup_rows}])
    track = TrackMetadata(
        Path("Radiohead/In Rainbows/01.flac"),
        "15 Step",
        "Radiohead",
        "In Rainbows",
        "Radiohead",
        2007,
        1,
        1,
        1,
        1,
        237_300,
    )
    group = AlbumGroup("In Rainbows", "Radiohead", 2007, (track.path,), (track,))
    client = AppleCatalogClient(
        country="US",
        cache_dir=tmp_path / "cache",
        session=session,
        api_interval=0,
    )

    first = client.find_candidates(group)
    second = client.find_candidates(group)

    assert [album.collection_id for album in first] == [42]
    assert second == first
    assert len(session.calls) == 2
    assert session.calls[0][1]["entity"] == "album"
    assert session.calls[1][1]["id"] == "42"


def test_catalog_client_prefers_exact_upc_lookup_when_barcode_is_tagged(tmp_path: Path) -> None:
    lookup_rows = [
        {
            "wrapperType": "collection",
            "collectionId": 99,
            "collectionName": "Tagged Album",
            "artistName": "Tagged Artist",
            "releaseDate": "2024-01-01T00:00:00Z",
            "trackCount": 1,
            "artworkUrl100": "https://is1-ssl.mzstatic.com/a/100x100bb.jpg",
        },
        {
            "wrapperType": "track",
            "kind": "song",
            "collectionId": 99,
            "collectionName": "Tagged Album",
            "artistName": "Tagged Artist",
            "trackName": "Tagged Song",
            "trackTimeMillis": 100_000,
            "discNumber": 1,
            "trackNumber": 1,
        },
    ]
    session = FakeSession([{"results": lookup_rows}])
    track = TrackMetadata(
        Path("song.flac"),
        "Tagged Song",
        "Tagged Artist",
        "Tagged Album",
        "Tagged Artist",
        2024,
        1,
        1,
        1,
        1,
        100_000,
        "012345678905",
    )
    group = AlbumGroup(
        "Tagged Album",
        "Tagged Artist",
        2024,
        (track.path,),
        (track,),
        barcode="012345678905",
    )
    client = AppleCatalogClient(cache_dir=tmp_path / "cache", session=session, api_interval=0)

    albums = client.find_candidates(group)

    assert [album.collection_id for album in albums] == [99]
    assert len(session.calls) == 1
    assert session.calls[0][0].endswith("/lookup")
    assert session.calls[0][1]["upc"] == "012345678905"
    assert albums[0].identifier_resolution == "embedded_upc"


def test_catalog_client_does_not_fuzzy_match_an_unresolved_upc(tmp_path: Path) -> None:
    session = FakeSession([{"results": []}])
    track = TrackMetadata(
        Path("song.flac"),
        "Tagged Song",
        "Tagged Artist",
        "Tagged Album",
        "Tagged Artist",
        2024,
        1,
        1,
        1,
        1,
        100_000,
        "012345678905",
    )
    group = AlbumGroup(
        "Tagged Album",
        "Tagged Artist",
        2024,
        (track.path,),
        (track,),
        barcode="012345678905",
    )
    client = AppleCatalogClient(cache_dir=tmp_path / "cache", session=session, api_interval=0)

    assert client.find_candidates(group) == []
    assert len(session.calls) == 1
    assert client.last_identifier_warnings == (
        "the embedded UPC returned no usable complete Apple album",
    )


def test_catalog_client_returns_exact_upc_results_without_legacy_identity_gates(
    tmp_path: Path,
) -> None:
    wrong_upc_rows = [
        {
            "wrapperType": "collection",
            "collectionId": 98,
            "collectionName": "Wrong Parent Album",
            "artistName": "Tagged Artist",
            "releaseDate": "2024-01-01T00:00:00Z",
            "trackCount": 2,
            "artworkUrl100": "https://is1-ssl.mzstatic.com/a/100x100bb.jpg",
        },
        *(
            {
                "wrapperType": "track",
                "kind": "song",
                "collectionId": 98,
                "artistName": "Tagged Artist",
                "trackName": title,
                "trackTimeMillis": 100_000 + number,
                "discNumber": 1,
                "trackNumber": number,
            }
            for number, title in enumerate(("Wrong Song", "Tagged Song"), start=1)
        ),
    ]
    search_rows = [
        {
            "collectionId": 99,
            "collectionName": "Tagged Album",
            "artistName": "Tagged Artist",
            "trackCount": 1,
            "artworkUrl100": "https://is1-ssl.mzstatic.com/a/100x100bb.jpg",
        }
    ]
    correct_lookup_rows = [
        {
            "wrapperType": "collection",
            "collectionId": 99,
            "collectionName": "Tagged Album",
            "artistName": "Tagged Artist",
            "releaseDate": "2024-01-01T00:00:00Z",
            "trackCount": 1,
            "artworkUrl100": "https://is1-ssl.mzstatic.com/a/100x100bb.jpg",
        },
        {
            "wrapperType": "track",
            "kind": "song",
            "collectionId": 99,
            "artistName": "Tagged Artist",
            "trackName": "Tagged Song",
            "trackTimeMillis": 100_000,
            "discNumber": 1,
            "trackNumber": 1,
        },
    ]
    session = FakeSession(
        [
            {"results": wrong_upc_rows},
            {"results": search_rows},
            {"results": correct_lookup_rows},
        ]
    )
    track = TrackMetadata(
        path=Path("song.flac"),
        title="Tagged Song",
        artist="Tagged Artist",
        album="Tagged Album",
        album_artist="Tagged Artist",
        year=2024,
        track_number=1,
        track_total=1,
        disc_number=1,
        disc_total=1,
        duration_ms=100_000,
        barcode="012345678905",
        musicbrainz_release_id="12345678-1234-5678-9234-567812345678",
        musicbrainz_recording_id="abcdefab-cdef-4abc-8def-abcdefabcdef",
    )
    group = AlbumGroup(
        "Tagged Album",
        "Tagged Artist",
        2024,
        (track.path,),
        (track,),
        barcode="012345678905",
        musicbrainz_release_id="12345678-1234-5678-9234-567812345678",
        musicbrainz_provenance_complete=True,
    )
    musicbrainz = FakeMusicBrainzClient(None)
    client = AppleCatalogClient(
        cache_dir=tmp_path / "cache",
        session=session,
        musicbrainz_client=musicbrainz,
        api_interval=0,
    )

    albums = client.find_candidates(group)

    assert {album.collection_id for album in albums} == {98}
    assert len(session.calls) == 1
    assert session.calls[0][1]["upc"] == "012345678905"
    assert musicbrainz.calls == [group.musicbrainz_release_id]


def _musicbrainz_group() -> AlbumGroup:
    release_id = "12345678-1234-5678-9234-567812345678"
    track = TrackMetadata(
        path=Path("song.flac"),
        title="Local Presentation",
        artist="Tagged Artist",
        album="Tagged Album",
        album_artist="Tagged Artist",
        year=2024,
        track_number=1,
        track_total=1,
        disc_number=1,
        disc_total=1,
        duration_ms=100_000,
        musicbrainz_release_id=release_id,
    )
    return AlbumGroup(
        "Tagged Album",
        "Tagged Artist",
        2024,
        (track.path,),
        (track,),
        musicbrainz_release_id=release_id,
    )


def test_catalog_client_uses_musicbrainz_apple_relation_before_text_search(
    tmp_path: Path,
) -> None:
    group = _musicbrainz_group()
    recording_id = "abcdefab-cdef-4abc-8def-abcdefabcdef"
    group = replace(
        group,
        logical_tracks=(replace(group.logical_tracks[0], musicbrainz_recording_id=recording_id),),
        musicbrainz_provenance_complete=True,
    )
    relation = MusicBrainzRelease(
        release_id=group.musicbrainz_release_id or "",
        title="Canonical Album",
        artist="Canonical Artist",
        release_year=2024,
        track_count=1,
        barcode=None,
        apple_collection_ids=(99,),
        recording_ids=(recording_id,),
    )
    musicbrainz = FakeMusicBrainzClient(relation)
    session = FakeSession(
        [
            {
                "results": [
                    {
                        "wrapperType": "collection",
                        "collectionId": 99,
                        "collectionName": "Apple Presentation",
                        "artistName": "Apple Artist",
                        "releaseDate": "2024-01-01T00:00:00Z",
                        "trackCount": 1,
                        "artworkUrl100": "https://is1-ssl.mzstatic.com/a/100x100bb.jpg",
                    },
                    {
                        "wrapperType": "track",
                        "kind": "song",
                        "collectionId": 99,
                        "artistName": "Apple Artist",
                        "trackName": "Apple Track Presentation",
                        "trackTimeMillis": 100_000,
                        "discNumber": 1,
                        "trackNumber": 1,
                    },
                ]
            }
        ]
    )
    client = AppleCatalogClient(
        cache_dir=tmp_path / "cache",
        session=session,
        musicbrainz_client=musicbrainz,
        api_interval=0,
    )

    albums = client.find_candidates(group)

    assert [album.collection_id for album in albums] == [99]
    assert albums[0].verified_musicbrainz_release_id == group.musicbrainz_release_id
    assert albums[0].identifier_resolution == "musicbrainz_apple_relation"
    assert albums[0].musicbrainz_recordings_verified is True
    assert musicbrainz.calls == [group.musicbrainz_release_id]
    assert session.calls[0][1]["id"] == "99"


def test_catalog_client_preserves_resolved_musicbrainz_identity_through_search(
    tmp_path: Path,
) -> None:
    group = _musicbrainz_group()
    release = MusicBrainzRelease(
        release_id=group.musicbrainz_release_id or "",
        title="Canonical Album",
        artist="Canonical Artist",
        release_year=2024,
        track_count=1,
        barcode=None,
    )
    musicbrainz = FakeMusicBrainzClient(release)
    search_rows = [
        {
            "collectionId": 99,
            "collectionName": "Canonical Album",
            "artistName": "Canonical Artist",
            "trackCount": 1,
            "artworkUrl100": "https://is1-ssl.mzstatic.com/a/100x100bb.jpg",
        }
    ]
    lookup_rows = [
        {
            "wrapperType": "collection",
            "collectionId": 99,
            "collectionName": "Canonical Album",
            "artistName": "Canonical Artist",
            "releaseDate": "2024-01-01T00:00:00Z",
            "trackCount": 1,
            "artworkUrl100": "https://is1-ssl.mzstatic.com/a/100x100bb.jpg",
        },
        {
            "wrapperType": "track",
            "kind": "song",
            "collectionId": 99,
            "artistName": "Canonical Artist",
            "trackName": "Canonical Track",
            "trackTimeMillis": 100_000,
            "discNumber": 1,
            "trackNumber": 1,
        },
    ]
    session = FakeSession([{"results": search_rows}, {"results": lookup_rows}])
    client = AppleCatalogClient(
        cache_dir=tmp_path / "cache",
        session=session,
        musicbrainz_client=musicbrainz,
        api_interval=0,
    )

    albums = client.find_candidates(group)
    decision = choose_match(group, albums)

    assert [album.collection_id for album in albums] == [99]
    assert albums[0].identifier_resolution == "musicbrainz_search"
    assert decision.status == "matched"
    assert decision.match is not None
    assert any("resolved MusicBrainz identity" in warning for warning in decision.match.warnings)


def test_catalog_client_never_uses_local_song_fallback_for_resolved_mbid(
    tmp_path: Path,
) -> None:
    group = _musicbrainz_group()
    release = MusicBrainzRelease(
        release_id=group.musicbrainz_release_id or "",
        title="Canonical Album",
        artist="Canonical Artist",
        release_year=2024,
        track_count=1,
        barcode=None,
    )
    session = FakeSession([{"results": []}])
    client = AppleCatalogClient(
        cache_dir=tmp_path / "cache",
        session=session,
        musicbrainz_client=FakeMusicBrainzClient(release),
        api_interval=0,
    )

    assert client.find_candidates(group) == []
    assert len(session.calls) == 1
    assert session.calls[0][1]["entity"] == "album"
    assert session.calls[0][1]["term"] == "Canonical Artist Canonical Album"


def test_catalog_client_blocks_conflicting_embedded_and_musicbrainz_barcodes(
    tmp_path: Path,
) -> None:
    group = replace(_musicbrainz_group(), barcode="012345678905")
    release = MusicBrainzRelease(
        release_id=group.musicbrainz_release_id or "",
        title="Canonical Album",
        artist="Canonical Artist",
        release_year=2024,
        track_count=1,
        barcode="4006381333931",
        apple_collection_ids=(99,),
    )
    session = FakeSession([{"results": []}])
    client = AppleCatalogClient(
        cache_dir=tmp_path / "cache",
        session=session,
        musicbrainz_client=FakeMusicBrainzClient(release),
        api_interval=0,
    )

    assert client.find_candidates(group) == []
    assert len(session.calls) == 1
    assert client.last_identifier_warnings[-1] == (
        "the embedded UPC conflicts with the resolved MusicBrainz release barcode"
    )


def test_catalog_client_blocks_recording_mbids_outside_the_resolved_release(
    tmp_path: Path,
) -> None:
    group = _musicbrainz_group()
    recording_id = "abcdefab-cdef-4abc-8def-abcdefabcdef"
    track = replace(group.logical_tracks[0], musicbrainz_recording_id=recording_id)
    group = replace(
        group,
        logical_tracks=(track,),
        musicbrainz_provenance_complete=True,
    )
    release = MusicBrainzRelease(
        release_id=group.musicbrainz_release_id or "",
        title="Canonical Album",
        artist="Canonical Artist",
        release_year=2024,
        track_count=1,
        barcode=None,
        recording_ids=("12345678-1234-4abc-8def-123456789abc",),
    )
    session = FakeSession([])
    client = AppleCatalogClient(
        cache_dir=tmp_path / "cache",
        session=session,
        musicbrainz_client=FakeMusicBrainzClient(release),
        api_interval=0,
    )

    assert client.find_candidates(group) == []
    assert session.calls == []
    assert client.last_identifier_warnings == (
        "the embedded recording MBIDs could not be verified against the resolved "
        "MusicBrainz release (including possible merged aliases)",
    )


def test_catalog_client_does_not_search_apple_when_musicbrainz_cannot_resolve(
    tmp_path: Path,
) -> None:
    group = _musicbrainz_group()
    musicbrainz = FakeMusicBrainzClient(None)
    session = FakeSession([])
    client = AppleCatalogClient(
        cache_dir=tmp_path / "cache",
        session=session,
        musicbrainz_client=musicbrainz,
        api_interval=0,
    )

    assert client.find_candidates(group) == []
    assert musicbrainz.calls == [group.musicbrainz_release_id]
    assert session.calls == []
    assert client.last_identifier_warnings == (
        "MusicBrainz did not resolve the embedded release MBID; no unverified Apple "
        "candidate was trusted",
    )


def test_catalog_client_stops_before_network_on_grouped_identifier_conflicts(
    tmp_path: Path,
) -> None:
    conflict = "conflicting UPC/barcode tags within the album group"
    group = replace(_musicbrainz_group(), identifier_conflicts=(conflict,))
    session = FakeSession([])
    musicbrainz = FakeMusicBrainzClient(None)
    client = AppleCatalogClient(
        cache_dir=tmp_path / "cache",
        session=session,
        musicbrainz_client=musicbrainz,
        api_interval=0,
    )

    assert client.find_candidates(group) == []
    assert session.calls == []
    assert musicbrainz.calls == []
    assert client.last_identifier_warnings == (conflict,)


def test_catalog_client_uses_musicbrainz_barcode_as_an_apple_crosswalk(
    tmp_path: Path,
) -> None:
    group = _musicbrainz_group()
    release = MusicBrainzRelease(
        release_id=group.musicbrainz_release_id or "",
        title="Canonical Album",
        artist="Canonical Artist",
        release_year=2024,
        track_count=1,
        barcode="012345678905",
    )
    musicbrainz = FakeMusicBrainzClient(release)
    lookup_rows = [
        {
            "wrapperType": "collection",
            "collectionId": 99,
            "collectionName": "Apple Presentation",
            "artistName": "Apple Artist",
            "releaseDate": "2024-01-01T00:00:00Z",
            "trackCount": 1,
            "artworkUrl100": "https://is1-ssl.mzstatic.com/a/100x100bb.jpg",
        },
        {
            "wrapperType": "track",
            "kind": "song",
            "collectionId": 99,
            "artistName": "Apple Artist",
            "trackName": "Apple Track Presentation",
            "trackTimeMillis": 100_000,
            "discNumber": 1,
            "trackNumber": 1,
        },
    ]
    session = FakeSession([{"results": lookup_rows}])
    client = AppleCatalogClient(
        cache_dir=tmp_path / "cache",
        session=session,
        musicbrainz_client=musicbrainz,
        api_interval=0,
    )

    albums = client.find_candidates(group)

    assert [album.collection_id for album in albums] == [99]
    assert albums[0].verified_barcode == "012345678905"
    assert albums[0].verified_musicbrainz_release_id == group.musicbrainz_release_id
    assert albums[0].identifier_resolution == "musicbrainz_barcode"
    assert session.calls[0][1]["upc"] == "012345678905"


def test_catalog_client_falls_back_to_a_distinctive_song_when_album_search_misses(
    tmp_path: Path,
) -> None:
    song_search_rows = [
        {
            "wrapperType": "track",
            "kind": "song",
            "collectionId": 42,
            "collectionName": "In Rainbows",
            "trackName": "15 Step",
            "artistName": "Radiohead",
            "artworkUrl100": "https://is1-ssl.mzstatic.com/a/100x100bb.jpg",
        }
    ]
    lookup_rows = [
        {
            "wrapperType": "collection",
            "collectionId": 42,
            "collectionName": "In Rainbows",
            "artistName": "Radiohead",
            "releaseDate": "2007-01-01T00:00:00Z",
            "trackCount": 1,
            "artworkUrl100": "https://is1-ssl.mzstatic.com/a/100x100bb.jpg",
        },
        {
            "wrapperType": "track",
            "kind": "song",
            "collectionId": 42,
            "collectionName": "In Rainbows",
            "trackName": "15 Step",
            "artistName": "Radiohead",
            "trackTimeMillis": 237_293,
            "discNumber": 1,
            "trackNumber": 1,
        },
    ]
    session = FakeSession(
        [{"results": []}, {"results": song_search_rows}, {"results": lookup_rows}]
    )
    track = TrackMetadata(
        Path("song.flac"),
        "15 Step (Album Version)",
        "Radiohead",
        "In Rainbows",
        "Radiohead",
        2007,
        1,
        1,
        1,
        1,
        237_300,
    )
    group = AlbumGroup("In Rainbows", "Radiohead", 2007, (track.path,), (track,))
    client = AppleCatalogClient(cache_dir=tmp_path / "cache", session=session, api_interval=0)

    albums = client.find_candidates(group)

    assert [album.collection_id for album in albums] == [42]
    assert len(session.calls) == 3
    assert session.calls[1][1]["entity"] == "song"
    assert "15 Step" in str(session.calls[1][1]["term"])
    assert "Album Version" not in str(session.calls[1][1]["term"])
