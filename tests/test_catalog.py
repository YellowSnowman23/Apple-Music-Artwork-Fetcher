import json
from pathlib import Path

from apple_artwork import (
    AlbumGroup,
    AppleCatalogClient,
    TrackMetadata,
    candidate_ids_from_album_search,
    catalog_albums_from_lookup,
)


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
    client = AppleCatalogClient(cache_dir=tmp_path / "cache", session=session, api_interval=0)

    albums = client.find_candidates(group)

    assert [album.collection_id for album in albums] == [42]
    assert len(session.calls) == 3
    assert session.calls[1][1]["entity"] == "song"
    assert "15 Step" in str(session.calls[1][1]["term"])
