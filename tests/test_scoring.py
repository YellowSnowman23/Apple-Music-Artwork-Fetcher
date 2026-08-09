from dataclasses import replace
from pathlib import Path

from apple_artwork import (
    AlbumGroup,
    CatalogAlbum,
    CatalogTrack,
    TrackMetadata,
    choose_match,
    score_candidate,
)


def local_group(artist: str = "Alpha", album: str = "Greatest Hits") -> AlbumGroup:
    tracks = tuple(
        TrackMetadata(
            path=Path(f"{artist}/{album}/{number:02}.flac"),
            title=title,
            artist=artist,
            album=album,
            album_artist=artist,
            year=2020,
            track_number=number,
            track_total=3,
            disc_number=1,
            disc_total=1,
            duration_ms=duration,
        )
        for number, (title, duration) in enumerate(
            [("First Light", 180_000), ("Home Again", 201_000), ("Last Dance", 242_000)],
            start=1,
        )
    )
    return AlbumGroup(
        album=album,
        album_artist=artist,
        year=2020,
        files=tuple(track.path for track in tracks),
        logical_tracks=tracks,
    )


def catalog_album(
    artist: str, album: str = "Greatest Hits", collection_id: int = 1
) -> CatalogAlbum:
    return CatalogAlbum(
        collection_id=collection_id,
        album=album,
        artist=artist,
        release_year=2020,
        artwork_url="https://is1-ssl.mzstatic.com/image/thumb/Music/test.jpg/100x100bb.jpg",
        track_count=3,
        tracks=(
            CatalogTrack("First Light", artist, 180_100, 1, 1),
            CatalogTrack("Home Again", artist, 200_900, 1, 2),
            CatalogTrack("Last Dance", artist, 242_200, 1, 3),
        ),
    )


def test_exact_album_title_cannot_override_a_different_artist() -> None:
    score = score_candidate(local_group(), catalog_album("Completely Different Artist"))

    assert score.eligible is False
    assert "artist mismatch" in score.reasons


def test_trailing_album_version_annotation_does_not_block_tracklist_match() -> None:
    group = local_group()
    annotated_tracks = tuple(
        replace(track, title=f"{track.title} (Album Version)") for track in group.logical_tracks
    )
    annotated_group = replace(
        group,
        files=tuple(track.path for track in annotated_tracks),
        logical_tracks=annotated_tracks,
    )

    score = score_candidate(annotated_group, catalog_album("Alpha"))

    assert score.eligible is True
    assert score.components["track_coverage"] == 1.0


def test_matching_artist_and_album_still_require_tracklist_evidence() -> None:
    candidate = CatalogAlbum(
        collection_id=2,
        album="Greatest Hits",
        artist="Alpha",
        release_year=2020,
        artwork_url="https://example.invalid/art.jpg",
        track_count=3,
        tracks=(
            CatalogTrack("Unrelated One", "Alpha", 130_000, 1, 1),
            CatalogTrack("Unrelated Two", "Alpha", 140_000, 1, 2),
            CatalogTrack("Unrelated Three", "Alpha", 150_000, 1, 3),
        ),
    )

    score = score_candidate(local_group(), candidate)

    assert score.eligible is False
    assert "tracklist mismatch" in score.reasons
    assert score.components["track_coverage"] == 0.0


def test_choose_match_uses_the_verified_tracklist_not_search_result_order() -> None:
    correct = catalog_album("Alpha", collection_id=22)
    wrong = catalog_album("Completely Different Artist", collection_id=11)

    decision = choose_match(local_group(), [wrong, correct])

    assert decision.status == "matched"
    assert decision.match is not None
    assert decision.match.candidate.collection_id == 22
    assert decision.match.components["track_coverage"] == 1.0


def test_choose_match_abstains_when_two_editions_are_indistinguishable() -> None:
    first = catalog_album("Alpha", collection_id=101)
    second = catalog_album("Alpha", collection_id=202)

    decision = choose_match(local_group(), [first, second])

    assert decision.status == "ambiguous"
    assert decision.match is None
    assert "margin" in decision.reason


def test_partial_tracklist_cannot_match_a_larger_deluxe_release() -> None:
    base = catalog_album("Alpha", collection_id=303)
    candidate = CatalogAlbum(
        collection_id=base.collection_id,
        album="Greatest Hits (Deluxe Edition)",
        artist=base.artist,
        release_year=base.release_year,
        artwork_url=base.artwork_url,
        track_count=5,
        tracks=(
            *base.tracks,
            CatalogTrack("Bonus One", "Alpha", 190_000, 1, 4),
            CatalogTrack("Bonus Two", "Alpha", 210_000, 1, 5),
        ),
    )

    score = score_candidate(local_group(), candidate)

    assert score.eligible is False
    assert "edition/version conflict" in score.reasons
    assert "tracklist coverage below 85%" in score.reasons


def test_five_second_duration_difference_is_not_a_strong_track_match() -> None:
    base = catalog_album("Alpha", collection_id=404)
    candidate = CatalogAlbum(
        collection_id=base.collection_id,
        album=base.album,
        artist=base.artist,
        release_year=base.release_year,
        artwork_url=base.artwork_url,
        track_count=base.track_count,
        tracks=(
            CatalogTrack("First Light", "Alpha", 185_000, 1, 1),
            *base.tracks[1:],
        ),
    )

    score = score_candidate(local_group(), candidate)

    assert score.eligible is False
    assert score.components["track_coverage"] < 0.85


def test_similar_but_different_track_title_is_not_counted_as_strong_evidence() -> None:
    base = catalog_album("Alpha", collection_id=505)
    candidate = CatalogAlbum(
        collection_id=base.collection_id,
        album=base.album,
        artist=base.artist,
        release_year=base.release_year,
        artwork_url=base.artwork_url,
        track_count=base.track_count,
        tracks=(
            CatalogTrack("First Light II", "Alpha", 180_100, 1, 1),
            *base.tracks[1:],
        ),
    )

    score = score_candidate(local_group(), candidate)

    assert score.eligible is False
    assert score.components["track_coverage"] < 0.85


def test_two_track_release_abstains_without_a_verified_identifier() -> None:
    full = local_group()
    tracks = full.logical_tracks[:2]
    short_group = AlbumGroup(
        album=full.album,
        album_artist=full.album_artist,
        year=full.year,
        files=tuple(track.path for track in tracks),
        logical_tracks=tracks,
    )
    candidate = CatalogAlbum(
        collection_id=606,
        album=full.album,
        artist=full.album_artist,
        release_year=full.year,
        artwork_url="https://example.invalid/art.jpg",
        track_count=2,
        tracks=tuple(
            CatalogTrack(
                track.title,
                track.artist,
                track.duration_ms,
                track.disc_number,
                track.track_number,
            )
            for track in tracks
        ),
    )

    decision = choose_match(short_group, [candidate])

    assert decision.status == "no_match"
    assert decision.scores
    assert "fewer than three strong tracks" in decision.scores[0].reasons


def test_declared_incomplete_local_album_is_never_auto_matched() -> None:
    full = local_group()
    incomplete_tracks = tuple(replace(track, track_total=5) for track in full.logical_tracks)
    incomplete = replace(
        full,
        files=tuple(track.path for track in incomplete_tracks),
        logical_tracks=incomplete_tracks,
    )

    score = score_candidate(incomplete, catalog_album("Alpha", collection_id=707))

    assert score.eligible is False
    assert "local tracklist appears incomplete" in score.reasons
