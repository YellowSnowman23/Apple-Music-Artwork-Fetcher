import os
from pathlib import Path

import pytest

from apple_artwork import AlbumGroup, AppleCatalogClient, ArtworkDownloader, TrackMetadata

pytestmark = pytest.mark.skipif(
    os.environ.get("APPLE_ARTWORK_LIVE_TEST") != "1",
    reason="set APPLE_ARTWORK_LIVE_TEST=1 to contact Apple's public API/CDN",
)


def test_live_apple_search_lookup_and_high_resolution_cdn_download(tmp_path: Path) -> None:
    tracks = (
        TrackMetadata(
            Path("01.flac"),
            "15 Step",
            "Radiohead",
            "In Rainbows",
            "Radiohead",
            2007,
            1,
            10,
            1,
            1,
            237_000,
        ),
        TrackMetadata(
            Path("02.flac"),
            "Bodysnatchers",
            "Radiohead",
            "In Rainbows",
            "Radiohead",
            2007,
            2,
            10,
            1,
            1,
            242_000,
        ),
        TrackMetadata(
            Path("03.flac"),
            "Nude",
            "Radiohead",
            "In Rainbows",
            "Radiohead",
            2007,
            3,
            10,
            1,
            1,
            255_000,
        ),
    )
    group = AlbumGroup(
        "In Rainbows",
        "Radiohead",
        2007,
        tuple(track.path for track in tracks),
        tracks,
    )
    cache_dir = tmp_path / "cache"
    client = AppleCatalogClient(
        country="US",
        cache_dir=cache_dir,
        api_interval=0.1,
    )

    candidates = client.find_candidates(group)
    album = next(
        candidate
        for candidate in candidates
        if candidate.artist.casefold() == "radiohead"
        and candidate.album.casefold() == "in rainbows"
    )

    assert album.collection_id > 0
    assert len(album.tracks) >= 10
    assert album.artwork_url.startswith("https://")

    artwork = ArtworkDownloader(cache_dir=cache_dir, cdn_interval=0.1).fetch(
        album.collection_id,
        album.artwork_url,
    )

    assert artwork.mime in {"image/jpeg", "image/png"}
    assert min(artwork.width, artwork.height) >= 1_000
    assert len(artwork.data) > 10_000
    assert "mzstatic.com" in artwork.source_url
