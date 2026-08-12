from __future__ import annotations

import json
from pathlib import Path

import pytest

from apple_music_artwork.constants import USER_AGENT
from apple_music_artwork.musicbrainz import (
    MusicBrainzClient,
    _apple_collection_id,
    parse_musicbrainz_release,
)

RELEASE_ID = "12345678-1234-5678-9234-567812345678"
CANONICAL_RELEASE_ID = "87654321-4321-8765-9321-876543218765"


def release_payload() -> dict[str, object]:
    return {
        "id": RELEASE_ID,
        "title": "Trusted Album",
        "date": "2024-02-03",
        "barcode": "012345678905",
        "artist-credit": [{"name": "Trusted Artist", "joinphrase": ""}],
        "media": [
            {
                "track-count": 2,
                "tracks": [
                    {"recording": {"id": "abcdefab-cdef-4abc-8def-abcdefabcdef"}},
                    {"recording": {"id": "12345678-1234-4abc-8def-123456789abc"}},
                ],
            }
        ],
        "relations": [
            {
                "target-type": "url",
                "type": "streaming music",
                "url": {"resource": "https://music.apple.com/us/album/trusted-album/1234567890"},
            }
        ],
    }


class FakeResponse:
    def __init__(
        self,
        payload: dict[str, object] | None = None,
        *,
        status_code: int = 200,
        url: str,
        location: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.url = url
        self.headers: dict[str, str] = {"Content-Type": "application/json"}
        if location is not None:
            self.headers["Location"] = location
        self.body = json.dumps(payload or {}).encode("utf-8")

    def iter_content(self, chunk_size: int = 65_536):
        for offset in range(0, len(self.body), chunk_size):
            yield self.body[offset : offset + chunk_size]

    def close(self) -> None:
        return None

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.headers: dict[str, str] = {}

    def get(self, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append((url, dict(kwargs)))
        return self.responses.pop(0)


def test_release_parser_extracts_barcode_track_count_and_apple_relation() -> None:
    release = parse_musicbrainz_release(
        release_payload(),
        expected_release_id=RELEASE_ID,
    )

    assert release.release_id == RELEASE_ID
    assert release.barcode == "012345678905"
    assert release.track_count == 2
    assert release.release_year == 2024
    assert release.apple_collection_ids == (1234567890,)
    assert release.recording_ids == (
        "abcdefab-cdef-4abc-8def-abcdefabcdef",
        "12345678-1234-4abc-8def-123456789abc",
    )


def test_release_parser_rejects_declared_medium_track_count_mismatch() -> None:
    payload = release_payload()
    payload["media"][0]["track-count"] = 3  # type: ignore[index]

    with pytest.raises(ValueError, match="inconsistent medium track count"):
        parse_musicbrainz_release(payload, expected_release_id=RELEASE_ID)


def test_release_parser_rejects_track_count_recording_id_disagreement() -> None:
    payload = release_payload()
    del payload["media"][0]["tracks"][1]["recording"]["id"]  # type: ignore[index]

    with pytest.raises(ValueError, match="inconsistent release track topology"):
        parse_musicbrainz_release(payload, expected_release_id=RELEASE_ID)


def test_musicbrainz_parser_ignores_non_album_apple_relations() -> None:
    payload = release_payload()
    payload["relations"] = [
        {
            "target-type": "url",
            "url": {"resource": "https://music.apple.com/us/artist/trusted-artist/1234567890"},
        }
    ]

    release = parse_musicbrainz_release(payload, expected_release_id=RELEASE_ID)

    assert release.apple_collection_ids == ()


@pytest.mark.parametrize(
    ("resource", "expected"),
    [
        ("https://music.apple.com/album/trusted-album/1234567890", 1234567890),
        ("https://music.apple.com/us/album/trusted-album/1234567890/", 1234567890),
        ("https://music.apple.com/gb/album/1234567890", 1234567890),
        ("https://music.apple.com/us/album/trusted-album/id1234567890", 1234567890),
        ("https://itunes.apple.com/album/id1234567890", 1234567890),
        ("https://itunes.apple.com/gb/album/id1234567890/?uo=4", 1234567890),
        (
            "https://itunes.apple.com/WebObjects/MZStore.woa/wa/viewAlbum?id=318293520&s=143462",
            318293520,
        ),
        (
            "https://itunes.apple.com/us/wa/viewAlbum?s=143441&id=318293520",
            318293520,
        ),
    ],
)
def test_apple_collection_id_accepts_only_canonical_album_routes(
    resource: str,
    expected: int,
) -> None:
    assert _apple_collection_id(resource) == expected


@pytest.mark.parametrize(
    "resource",
    [
        "https://music.apple.com/us/album/trusted-album/1234567890/track/999",
        "https://music.apple.com/us/artist/trusted-artist/1234567890",
        "https://music.apple.com/store/us/album/trusted-album/1234567890",
        "https://music.apple.com/us/album//trusted-album/1234567890",
        "https://music.apple.com/us/album/id0",
        (
            "https://itunes.apple.com/WebObjects/MZStore.woa/wa/"
            "viewAlbum/track?id=318293520&s=143462"
        ),
        ("https://itunes.apple.com/WebObjects/MZStore.woa/wa/viewAlbum?id=318293520"),
        (
            "https://itunes.apple.com/WebObjects/MZStore.woa/wa/"
            "viewAlbum?id=318293520&s=143462&entity=song"
        ),
        (
            "https://itunes.apple.com/WebObjects/MZStore.woa/wa/"
            "viewAlbum?id=318293520&id=999&s=143462"
        ),
        "https://itunes.apple.com/us/wa/viewAlbum?id=0&s=143441",
        "https://itunes.apple.com/us/wa/viewAlbum?id=318293520&s=us",
    ],
)
def test_apple_collection_id_rejects_noncanonical_album_routes(resource: str) -> None:
    assert _apple_collection_id(resource) is None


def test_musicbrainz_client_resolves_once_and_reuses_bound_cache(tmp_path: Path) -> None:
    url = f"https://musicbrainz.org/ws/2/release/{RELEASE_ID}"
    session = FakeSession([FakeResponse(release_payload(), url=url)])
    client = MusicBrainzClient(
        cache_dir=tmp_path,
        session=session,
        api_interval=1.0,
        max_retries=1,
    )

    first = client.resolve(RELEASE_ID)
    second = client.resolve(RELEASE_ID)

    assert first is not None
    assert second == first
    assert len(session.calls) == 1
    assert session.calls[0][1]["params"] == {
        "fmt": "json",
        "inc": "url-rels+artist-credits+recordings",
    }
    assert session.headers["User-Agent"] == USER_AGENT


def test_musicbrainz_redirect_cannot_leave_the_allowlisted_host(tmp_path: Path) -> None:
    url = f"https://musicbrainz.org/ws/2/release/{RELEASE_ID}"
    response = FakeResponse(
        status_code=302,
        url=url,
        location="https://example.com/stolen",
    )
    client = MusicBrainzClient(
        cache_dir=tmp_path,
        session=FakeSession([response]),
        api_interval=1.0,
        max_retries=1,
    )

    with pytest.raises(ValueError, match="allowlisted HTTPS MusicBrainz"):
        client.resolve(RELEASE_ID)


def test_musicbrainz_client_accepts_a_validated_merged_release_redirect(
    tmp_path: Path,
) -> None:
    requested_url = f"https://musicbrainz.org/ws/2/release/{RELEASE_ID}"
    canonical_url = f"https://musicbrainz.org/ws/2/release/{CANONICAL_RELEASE_ID}"
    payload = release_payload()
    payload["id"] = CANONICAL_RELEASE_ID
    session = FakeSession(
        [
            FakeResponse(status_code=301, url=requested_url, location=canonical_url),
            FakeResponse(payload, url=canonical_url),
        ]
    )
    client = MusicBrainzClient(
        cache_dir=tmp_path,
        session=session,
        api_interval=1.0,
        max_retries=1,
    )

    release = client.resolve(RELEASE_ID)

    assert release is not None
    assert release.release.release_id == CANONICAL_RELEASE_ID
    assert release.requested_release_id == RELEASE_ID
    assert len(session.calls) == 2
    assert session.calls[1][1]["params"] == {
        "fmt": "json",
        "inc": "url-rels+artist-credits+recordings",
    }


def test_musicbrainz_client_paces_each_redirect_hop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested_url = f"https://musicbrainz.org/ws/2/release/{RELEASE_ID}"
    canonical_url = f"https://musicbrainz.org/ws/2/release/{CANONICAL_RELEASE_ID}"
    payload = release_payload()
    payload["id"] = CANONICAL_RELEASE_ID
    session = FakeSession(
        [
            FakeResponse(status_code=301, url=requested_url, location=canonical_url),
            FakeResponse(payload, url=canonical_url),
        ]
    )
    client = MusicBrainzClient(
        cache_dir=tmp_path,
        session=session,
        api_interval=1.0,
        max_retries=1,
    )
    now = [100.0]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    monkeypatch.setattr("apple_music_artwork.musicbrainz.time.monotonic", lambda: now[0])
    monkeypatch.setattr("apple_music_artwork.musicbrainz.time.sleep", sleep)

    assert client.resolve(RELEASE_ID) is not None
    assert sleeps == [1.0]


def test_musicbrainz_redirect_adds_required_params_missing_from_location_query(
    tmp_path: Path,
) -> None:
    requested_url = f"https://musicbrainz.org/ws/2/release/{RELEASE_ID}"
    canonical_url = f"https://musicbrainz.org/ws/2/release/{CANONICAL_RELEASE_ID}?source=merge"
    payload = release_payload()
    payload["id"] = CANONICAL_RELEASE_ID
    session = FakeSession(
        [
            FakeResponse(status_code=301, url=requested_url, location=canonical_url),
            FakeResponse(payload, url=canonical_url),
        ]
    )
    client = MusicBrainzClient(
        cache_dir=tmp_path,
        session=session,
        api_interval=1.0,
        max_retries=1,
    )

    assert client.resolve(RELEASE_ID) is not None
    assert session.calls[1][1]["params"] == {
        "fmt": "json",
        "inc": "url-rels+artist-credits+recordings",
    }


def test_musicbrainz_redirect_rejects_changed_required_query_params(
    tmp_path: Path,
) -> None:
    requested_url = f"https://musicbrainz.org/ws/2/release/{RELEASE_ID}"
    canonical_url = f"https://musicbrainz.org/ws/2/release/{CANONICAL_RELEASE_ID}?fmt=xml"
    session = FakeSession(
        [FakeResponse(status_code=301, url=requested_url, location=canonical_url)]
    )
    client = MusicBrainzClient(
        cache_dir=tmp_path,
        session=session,
        api_interval=1.0,
        max_retries=1,
    )

    with pytest.raises(ValueError, match="changed required API query parameters"):
        client.resolve(RELEASE_ID)

    assert len(session.calls) == 1


def test_musicbrainz_redirect_accepts_raw_plus_encoded_required_inc(
    tmp_path: Path,
) -> None:
    requested_url = f"https://musicbrainz.org/ws/2/release/{RELEASE_ID}"
    canonical_url = (
        f"https://musicbrainz.org/ws/2/release/{CANONICAL_RELEASE_ID}"
        "?fmt=json&inc=url-rels+artist-credits+recordings"
    )
    payload = release_payload()
    payload["id"] = CANONICAL_RELEASE_ID
    session = FakeSession(
        [
            FakeResponse(status_code=301, url=requested_url, location=canonical_url),
            FakeResponse(payload, url=canonical_url),
        ]
    )
    client = MusicBrainzClient(
        cache_dir=tmp_path,
        session=session,
        api_interval=1.0,
        max_retries=1,
    )

    assert client.resolve(RELEASE_ID) is not None
    assert "params" not in session.calls[1][1]


def test_musicbrainz_client_reuses_cached_merged_release_alias(tmp_path: Path) -> None:
    requested_url = f"https://musicbrainz.org/ws/2/release/{RELEASE_ID}"
    canonical_url = f"https://musicbrainz.org/ws/2/release/{CANONICAL_RELEASE_ID}"
    payload = release_payload()
    payload["id"] = CANONICAL_RELEASE_ID
    session = FakeSession(
        [
            FakeResponse(status_code=301, url=requested_url, location=canonical_url),
            FakeResponse(payload, url=canonical_url),
        ]
    )
    client = MusicBrainzClient(
        cache_dir=tmp_path,
        session=session,
        api_interval=1.0,
        max_retries=1,
    )

    first = client.resolve(RELEASE_ID)
    second = client.resolve(RELEASE_ID)

    assert first is not None
    assert second == first
    assert second.release.release_id == CANONICAL_RELEASE_ID
    assert len(session.calls) == 2
    cache_envelope = json.loads(client._cache_path(RELEASE_ID).read_text(encoding="ascii"))
    assert cache_envelope["schema_version"] == 2
    assert cache_envelope["canonical_release_id"] == CANONICAL_RELEASE_ID


def test_musicbrainz_parser_rejects_a_cross_key_release() -> None:
    payload = release_payload()
    payload["id"] = "87654321-4321-8765-9321-876543218765"

    with pytest.raises(ValueError, match="different or invalid release ID"):
        parse_musicbrainz_release(payload, expected_release_id=RELEASE_ID)


def test_musicbrainz_client_rejects_wrong_content_type(tmp_path: Path) -> None:
    url = f"https://musicbrainz.org/ws/2/release/{RELEASE_ID}"
    response = FakeResponse(release_payload(), url=url)
    response.headers["Content-Type"] = "text/html"
    client = MusicBrainzClient(
        cache_dir=tmp_path,
        session=FakeSession([response]),
        api_interval=1.0,
        max_retries=1,
    )

    with pytest.raises(ValueError, match="unsupported Content-Type"):
        client.resolve(RELEASE_ID)


def test_musicbrainz_client_bounds_response_bytes(tmp_path: Path) -> None:
    url = f"https://musicbrainz.org/ws/2/release/{RELEASE_ID}"
    client = MusicBrainzClient(
        cache_dir=tmp_path,
        session=FakeSession([FakeResponse(release_payload(), url=url)]),
        api_interval=1.0,
        max_retries=1,
        max_response_bytes=32,
    )

    with pytest.raises(ValueError, match="exceeds"):
        client.resolve(RELEASE_ID)


def test_musicbrainz_cache_symlink_is_never_followed_or_replaced(tmp_path: Path) -> None:
    url = f"https://musicbrainz.org/ws/2/release/{RELEASE_ID}"
    session = FakeSession([FakeResponse(release_payload(), url=url)])
    client = MusicBrainzClient(
        cache_dir=tmp_path,
        session=session,
        api_interval=1.0,
        max_retries=1,
    )
    target = tmp_path / "outside.json"
    target.write_text("do not replace", encoding="ascii")
    cache_path = client._cache_path(RELEASE_ID)
    cache_path.parent.mkdir(parents=True)
    cache_path.symlink_to(target)

    with pytest.raises(OSError, match="not a regular file"):
        client.resolve(RELEASE_ID)

    assert target.read_text(encoding="ascii") == "do not replace"
    assert len(session.calls) == 1
