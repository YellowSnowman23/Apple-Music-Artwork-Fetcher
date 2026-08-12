from dataclasses import replace
from pathlib import Path

import pytest

import apple_artwork
from apple_artwork import (
    AlbumGroup,
    AppleCatalogClient,
    CatalogAlbum,
    CatalogTrack,
    TrackMetadata,
    candidate_ids_from_album_search,
    catalog_albums_from_lookup,
    choose_match,
    group_tracks,
    score_candidate,
)


def local_group(
    *,
    artist: str = "The National",
    album: str = "Shared Album",
    count: int = 3,
    barcode: str | None = None,
) -> AlbumGroup:
    tracks = tuple(
        TrackMetadata(
            path=Path(f"{artist}/{album}/{number:02}.flac"),
            title=f"Song {number}",
            artist=artist,
            album=album,
            album_artist=artist,
            year=2020,
            track_number=number,
            track_total=count,
            disc_number=1,
            disc_total=1,
            duration_ms=180_000 + number * 1_000,
            barcode=barcode,
        )
        for number in range(1, count + 1)
    )
    return AlbumGroup(
        album,
        artist,
        2020,
        tuple(track.path for track in tracks),
        tracks,
        barcode=barcode,
    )


def remote_album(
    *,
    collection_id: int = 1,
    artist: str = "The National",
    album: str = "Shared Album",
    count: int = 3,
    returned_count: int | None = None,
    track_artist: str | None = None,
    verified_barcode: str | None = None,
) -> CatalogAlbum:
    rows = returned_count if returned_count is not None else count
    return CatalogAlbum(
        collection_id=collection_id,
        album=album,
        artist=artist,
        release_year=2020,
        artwork_url="https://is1-ssl.mzstatic.com/image/thumb/test/100x100.jpg",
        track_count=count,
        tracks=tuple(
            CatalogTrack(
                f"Song {number}",
                track_artist or artist,
                180_000 + number * 1_000,
                1,
                number,
            )
            for number in range(1, rows + 1)
        ),
        verified_barcode=verified_barcode,
    )


def lookup_rows(
    collection_id: int,
    *,
    artist: str = "The National",
    album: str = "Shared Album",
    titles: tuple[str, ...] = ("Song 1", "Song 2", "Song 3"),
    declared_count: int | None = None,
) -> list[dict[str, object]]:
    declared = declared_count if declared_count is not None else len(titles)
    rows: list[dict[str, object]] = [
        {
            "wrapperType": "collection",
            "collectionType": "Album",
            "collectionId": collection_id,
            "collectionName": album,
            "artistName": artist,
            "releaseDate": "2020-01-01T00:00:00Z",
            "trackCount": declared,
            "artworkUrl100": "https://is1-ssl.mzstatic.com/image/thumb/test/100x100.jpg",
        }
    ]
    rows.extend(
        {
            "wrapperType": "track",
            "kind": "song",
            "collectionId": collection_id,
            "collectionName": album,
            "artistName": artist,
            "trackName": title,
            "trackTimeMillis": 180_000 + number * 1_000,
            "discNumber": 1,
            "trackNumber": number,
        }
        for number, title in enumerate(titles, start=1)
    )
    return rows


def test_album_artist_gate_requires_normalized_identity_not_fuzzy_nearness() -> None:
    score = score_candidate(local_group(), remote_album(artist="The National Parks"))

    assert score.eligible is False
    assert "artist mismatch" in score.reasons
    assert (
        candidate_ids_from_album_search(
            [
                {
                    "collectionId": 9,
                    "collectionName": "Shared Album",
                    "artistName": "The National Parks",
                    "artworkUrl100": "https://is1-ssl.mzstatic.com/a.jpg",
                }
            ],
            "The National",
            "Shared Album",
        )
        == []
    )


def test_per_track_artist_conflicts_are_hard_rejections() -> None:
    score = score_candidate(
        local_group(artist="Various Artists"),
        remote_album(artist="Various Artists", track_artist="Wrong Artist"),
    )

    assert score.eligible is False
    assert "track artist mismatch" in score.reasons
    assert score.components["track_artist"] == 0.0


def test_master_in_an_ordinary_title_is_not_a_remaster_qualifier() -> None:
    group = local_group(album="Master of Puppets")
    candidate = remote_album(album="Master of Puppets (Remastered)")

    score = score_candidate(group, candidate)

    assert score.eligible is False
    assert "edition/version conflict" in score.reasons


def test_different_remaster_years_are_conflicting_editions() -> None:
    group = local_group(album="Shared Album (2009 Remaster)")
    candidate = remote_album(album="Shared Album (2010 Remaster)")

    assert "edition/version conflict" in score_candidate(group, candidate).reasons


def test_declared_remote_count_is_the_coverage_denominator() -> None:
    score = score_candidate(local_group(), remote_album(count=10, returned_count=3))

    assert score.eligible is False
    assert "Apple tracklist appears incomplete" in score.reasons
    assert score.components["track_coverage"] == pytest.approx(0.3)


def test_lookup_requires_collection_row_and_complete_unique_positions() -> None:
    without_collection = lookup_rows(1)[1:]
    incomplete = lookup_rows(2, declared_count=4)
    duplicate = lookup_rows(3)
    duplicate[-1]["trackNumber"] = 2

    assert catalog_albums_from_lookup(without_collection) == []
    assert catalog_albums_from_lookup(incomplete) == []
    assert catalog_albums_from_lookup(duplicate) == []


def test_local_track_positions_must_be_unique_contiguous_and_within_totals() -> None:
    full = local_group()
    gap = replace(
        full,
        logical_tracks=(
            full.logical_tracks[0],
            replace(full.logical_tracks[1], track_number=3),
            replace(full.logical_tracks[2], track_number=4),
        ),
    )

    score = score_candidate(gap, remote_album())

    assert score.eligible is False
    assert "local tracklist appears incomplete" in score.reasons


def test_invalid_upc_cannot_bypass_short_release_guard() -> None:
    group = local_group(count=1, barcode="012345678906")
    decision = choose_match(group, [remote_album(count=1)])

    assert decision.status == "no_match"
    assert "fewer than three strong tracks" in decision.scores[0].reasons


@pytest.mark.parametrize("reverse", [False, True])
def test_valid_upc_requires_direct_apple_or_resolved_musicbrainz_provenance(
    reverse: bool,
) -> None:
    barcode = "012345678905"
    group = local_group(count=1, barcode=barcode)
    stale_search_candidate = remote_album(count=1)
    verified = remote_album(count=1, collection_id=2, verified_barcode=barcode)

    unverified_decision = choose_match(group, [stale_search_candidate])
    assert unverified_decision.status == "no_match"
    assert "candidate lacks resolved identifier provenance" in (
        unverified_decision.scores[0].reasons
    )

    candidates = [verified, stale_search_candidate]
    if reverse:
        candidates.reverse()
    verified_decision = choose_match(group, candidates)
    assert verified_decision.status == "matched"
    assert verified_decision.match is not None
    assert verified_decision.match.candidate.collection_id == 2
    assert verified_decision.match.components["verified_upc"] == 1.0


def test_release_mbid_groups_tracks_despite_conflicting_legacy_identity() -> None:
    release_id = "12345678-1234-5678-9234-567812345678"
    alpha = local_group(artist="Alpha", album="Album A", count=1).logical_tracks[0]
    beta = local_group(artist="Beta", album="Album B", count=1).logical_tracks[0]
    groups = group_tracks(
        [
            replace(alpha, musicbrainz_release_id=release_id),
            replace(beta, musicbrainz_release_id=release_id),
        ]
    )

    assert len(groups) == 1
    assert groups[0].musicbrainz_release_id == release_id
    assert groups[0].album_artist == "Alpha"
    assert groups[0].album == "Album A"
    assert groups[0].files == tuple(sorted((alpha.path, beta.path), key=str))


def test_placeholder_identifiers_are_discarded_instead_of_becoming_group_keys() -> None:
    alpha = local_group(artist="Alpha", album="Album A", count=1).logical_tracks[0]
    beta = local_group(artist="Beta", album="Album B", count=1).logical_tracks[0]

    groups = group_tracks(
        [
            replace(alpha, barcode="N/A", musicbrainz_release_id="not-a-uuid"),
            replace(beta, barcode="N/A", musicbrainz_release_id="not-a-uuid"),
        ]
    )

    assert len(groups) == 2
    assert all(group.barcode is None and group.musicbrainz_release_id is None for group in groups)


def test_huge_numeric_tag_is_bounded_without_big_integer_conversion() -> None:
    assert apple_artwork._number_pair("9" * 5000) == (None, None)


def test_path_filters_use_separator_aware_double_star_semantics() -> None:
    matches = apple_artwork._path_matches

    assert matches("Radiohead/Album/song.flac", "Radiohead/**")
    assert matches("Radiohead/Album/song.flac", "**/*.flac")
    assert matches("song.flac", "**/*.flac")
    assert not matches("Radiohead/Album/song.flac", "*.flac")
    assert matches("Radiohead/Singles/song.flac", "**/Singles/**")
    assert not matches("Radiohead/NotSingles/song.flac", "**/Singles/**")


def test_batch_lookup_retries_missing_collection_ids_individually(tmp_path: Path) -> None:
    group = local_group()
    client = AppleCatalogClient(cache_dir=tmp_path / "cache", api_interval=0)
    calls: list[dict[str, object]] = []

    def fake_request(url: str, params: dict[str, object]):
        del url
        calls.append(dict(params))
        if params.get("entity") == "album":
            return [
                {
                    "collectionId": 1,
                    "collectionName": group.album,
                    "artistName": group.album_artist,
                    "artworkUrl100": "https://is1-ssl.mzstatic.com/a.jpg",
                },
                {
                    "collectionId": 2,
                    "collectionName": group.album,
                    "artistName": group.album_artist,
                    "artworkUrl100": "https://is1-ssl.mzstatic.com/b.jpg",
                },
            ]
        if params.get("id") == "1,2":
            return lookup_rows(1)
        if params.get("id") == "2":
            return lookup_rows(2)
        raise AssertionError(params)

    client._request_results = fake_request  # type: ignore[method-assign]

    albums = client.find_candidates(group)

    assert [album.collection_id for album in albums] == [1, 2]
    assert any(call.get("id") == "2" for call in calls)


def test_song_fallback_runs_when_album_expansion_fails_identity_gates(tmp_path: Path) -> None:
    group = local_group()
    client = AppleCatalogClient(cache_dir=tmp_path / "cache", api_interval=0)
    song_searches = 0

    def fake_request(url: str, params: dict[str, object]):
        nonlocal song_searches
        del url
        if params.get("entity") == "album":
            return [
                {
                    "collectionId": 1,
                    "collectionName": group.album,
                    "artistName": group.album_artist,
                    "artworkUrl100": "https://is1-ssl.mzstatic.com/a.jpg",
                }
            ]
        if params.get("entity") == "song" and "term" in params:
            song_searches += 1
            anchor = group.logical_tracks[0]
            return [
                {
                    "kind": "song",
                    "collectionId": 2,
                    "collectionName": group.album,
                    "artistName": group.album_artist,
                    "trackName": anchor.title,
                }
            ]
        if params.get("id") == "1":
            return lookup_rows(1, titles=("Wrong 1", "Wrong 2", "Wrong 3"))
        if params.get("id") == "2":
            return lookup_rows(2)
        raise AssertionError(params)

    client._request_results = fake_request  # type: ignore[method-assign]

    albums = client.find_candidates(group)

    assert song_searches >= 1
    assert any(album.collection_id == 2 for album in albums)
