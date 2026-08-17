from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from apple_music_artwork.metadata import album_local_diagnostics, group_tracks
from apple_music_artwork.models import TrackMetadata


def _track(
    path: str,
    *,
    number: int,
    total: int,
    year: int = 2024,
    title: str | None = None,
) -> TrackMetadata:
    return TrackMetadata(
        path=Path(path),
        title=title or f"Song {number}",
        artist="Artist",
        album="Album",
        album_artist="Artist",
        year=year,
        track_number=number,
        track_total=total,
        disc_number=1,
        disc_total=1,
        duration_ms=180_000 + number,
    )


def test_normalized_tags_do_not_merge_distinct_sibling_release_folders() -> None:
    standard = replace(
        _track("Artist/Fall In Love/01.flac", number=1, total=1),
        album="Fall In Love",
        title="Fall In Love",
    )
    acoustic = replace(
        standard,
        path=Path("Artist/Fall In Love  (Acoustic)/01.flac"),
        album="Fall In Love ",
        title="Fall In Love (Acoustic)",
    )

    groups = group_tracks([acoustic, standard])

    assert len(groups) == 2
    assert {group.files for group in groups} == {
        (standard.path,),
        (acoustic.path,),
    }


def test_identifier_can_bridge_sibling_folders_for_duplicate_encodings() -> None:
    release_id = "12345678-1234-4abc-8def-123456789abc"
    recording_id = "abcdefab-cdef-4abc-8def-abcdefabcdef"
    first = replace(
        _track("Artist/Album FLAC/01.flac", number=1, total=1),
        musicbrainz_release_id=release_id,
        musicbrainz_recording_id=recording_id,
    )
    duplicate = replace(first, path=Path("Artist/Album MP3/01.mp3"))

    group = group_tracks([duplicate, first])[0]

    assert group.files == (first.path, duplicate.path)
    assert len(group.logical_tracks) == 1


def test_disc_subfolders_share_release_root_when_folder_and_tag_wording_differ() -> None:
    disc_one = replace(
        _track("Artist/Album/Disc 1/01.flac", number=1, total=1),
        album="Album (Deluxe Edition)",
        disc_number=1,
        disc_total=2,
    )
    disc_two = replace(
        disc_one,
        path=Path("Artist/Album/Disc 2/01.flac"),
        title="Second Song",
        disc_number=2,
    )

    group = group_tracks([disc_two, disc_one])[0]

    assert group.files == (disc_one.path, disc_two.path)
    assert len(group.logical_tracks) == 2


def test_same_release_root_keeps_conflicting_years_and_totals_in_one_group() -> None:
    tracks = [
        _track(
            f"Artist/Album/{number:02}.flac",
            number=number,
            total=14,
            year=2012,
        )
        for number in range(1, 15)
    ]
    tracks.append(
        _track(
            "Artist/Album/15.flac",
            number=15,
            total=15,
            year=2013,
        )
    )

    group = group_tracks(reversed(tracks))[0]
    diagnostics = album_local_diagnostics(group)

    assert len(group.files) == 15
    assert len(group.logical_tracks) == 15
    assert group.year is None
    assert diagnostics["declared_years"] == [2012, 2013]
    assert diagnostics["year_conflict"] is True
    assert diagnostics["track_total_conflicts"] == [
        {"disc": 1, "values": [14, 15]},
    ]
    assert diagnostics["missing_track_positions"] == []


def test_album_local_diagnostics_reports_missing_track_positions() -> None:
    tracks = [
        _track(f"Artist/Album/{number:02}.flac", number=number, total=14)
        for number in (*range(1, 12), 13, 14)
    ]

    diagnostics = album_local_diagnostics(group_tracks(tracks)[0])

    assert diagnostics["track_total_scope"] == "disc"
    assert diagnostics["declared_track_totals"] == [
        {"disc": 1, "values": [14]},
    ]
    assert diagnostics["missing_track_positions"] == [{"disc": 1, "track": 12}]
    assert diagnostics["missing_track_count"] == 1


def test_album_wide_multidisc_total_does_not_invent_per_disc_gaps() -> None:
    tracks = tuple(
        replace(
            _track(
                f"Artist/Album/Disc {disc}/{number:02}.flac",
                number=number,
                total=4,
            ),
            disc_number=disc,
            disc_total=2,
        )
        for disc in (1, 2)
        for number in (1, 2)
    )

    diagnostics = album_local_diagnostics(group_tracks(tracks)[0])

    assert diagnostics["track_total_scope"] == "album"
    assert diagnostics["position_layout"] == "per_disc"
    assert diagnostics["per_disc"] == [
        {
            "disc": 1,
            "present_positions": [1, 2],
            "declared_track_totals": [4],
            "effective_track_total": 4,
            "missing_positions": [],
            "out_of_range_positions": [],
        },
        {
            "disc": 2,
            "present_positions": [1, 2],
            "declared_track_totals": [4],
            "effective_track_total": 4,
            "missing_positions": [],
            "out_of_range_positions": [],
        },
    ]
    assert diagnostics["missing_track_positions"] == []
    assert diagnostics["missing_track_count"] == 0


def test_album_wide_global_positions_preserve_disc_breakdown() -> None:
    tracks = tuple(
        replace(
            _track(
                f"Artist/Album/Disc {disc}/{number:02}.flac",
                number=number,
                total=4,
            ),
            disc_number=disc,
            disc_total=2,
        )
        for disc, number in ((1, 1), (1, 2), (2, 3), (2, 4))
    )

    diagnostics = album_local_diagnostics(group_tracks(tracks)[0])

    assert diagnostics["track_total_scope"] == "album"
    assert diagnostics["position_layout"] == "global"
    assert [entry["present_positions"] for entry in diagnostics["per_disc"]] == [
        [1, 2],
        [3, 4],
    ]
    assert diagnostics["missing_track_count"] == 0
